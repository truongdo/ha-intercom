from __future__ import annotations

import asyncio
import logging
from typing import Callable
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from .audio.device import DeviceFactory
from .auth import (
    RateLimiter,
    SessionSigner,
    load_or_create_secret,
    load_users,
    verify_password,
)
from .config import Config
from .session import SessionManager
from .settings import Settings, load_settings, mask_token, save_settings
from .telegram import send_message

log = logging.getLogger(__name__)

COOKIE = "intercom_session"


def client_ip(conn: HTTPConnection) -> str:
    # uvicorn runs with proxy_headers=True and forwarded_allow_ips="127.0.0.1", so for requests
    # arriving from the local cloudflared it has already replaced the peer address with the
    # forwarded client address. Raw proxy headers are never read here.
    return conn.client.host if conn.client else ""


def origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if origin is None:
        return True
    host = ws.headers.get("host", "")
    return urlsplit(origin).netloc.lower() == host.lower() and bool(host)


def create_app(
    config: Config,
    device_factory: DeviceFactory,
    audio_probe: Callable[[bool], bool],
) -> Starlette:
    signer = SessionSigner(
        load_or_create_secret(config.auth.secret_file),
        max_age_s=config.auth.session_hours * 3600,
    )
    limiter = RateLimiter()
    manager = SessionManager(device_factory, config.audio.jitter_ms, config.session.idle_timeout_s)

    def current_user(conn: HTTPConnection) -> str | None:
        token = conn.cookies.get(COOKIE)
        return signer.read(token) if token else None

    async def login(request: Request) -> Response:
        ip = client_ip(request)
        if not limiter.allowed(ip):
            return JSONResponse({"error": "too_many_attempts"}, status_code=429)
        try:
            body = await request.json()
            username, password = str(body["username"]), str(body["password"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        try:
            users = await run_in_threadpool(load_users, config.auth.users_file)
        except OSError:
            log.exception("cannot read users file %s", config.auth.users_file)
            return JSONResponse({"error": "users_unavailable"}, status_code=503)
        if not await run_in_threadpool(verify_password, users, username, password):
            limiter.record_failure(ip)
            return JSONResponse({"error": "invalid_credentials"}, status_code=401)
        limiter.reset(ip)
        response = JSONResponse({"username": username})
        response.set_cookie(
            COOKIE,
            signer.issue(username),
            max_age=int(config.auth.session_hours * 3600),
            httponly=True,
            samesite="lax",
            secure=config.auth.secure_cookie,
        )
        return response

    async def logout(request: Request) -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    async def me(request: Request) -> Response:
        user = current_user(request)
        if user is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Never refresh PortAudio while a session holds the device; probe off the event loop.
        # The lock keeps a session from opening the device while a refresh is in progress.
        async with manager.device_lock:
            available = await run_in_threadpool(audio_probe, not manager.active)
        return JSONResponse(
            {"username": user, "audio_available": available}
        )

    async def get_admin_settings(request: Request) -> Response:
        if current_user(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        settings = await run_in_threadpool(load_settings, config.settings_file)
        return JSONResponse(
            {
                "chat_id": settings.telegram_chat_id,
                "token_masked": mask_token(settings.telegram_bot_token),
                "token_set": bool(settings.telegram_bot_token),
            }
        )

    async def save_admin_settings(request: Request) -> Response:
        if current_user(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            chat_id = str(body["chat_id"]).strip()
            token = str(body["token"]).strip()
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        current = await run_in_threadpool(load_settings, config.settings_file)
        if current.telegram_bot_token and token == mask_token(current.telegram_bot_token):
            token = current.telegram_bot_token
        try:
            await run_in_threadpool(
                save_settings,
                config.settings_file,
                Settings(telegram_bot_token=token, telegram_chat_id=chat_id),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    async def test_telegram(request: Request) -> Response:
        if current_user(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        settings = await run_in_threadpool(load_settings, config.settings_file)
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return JSONResponse({"ok": False, "reason": "not_configured"})
        ok, reason = await asyncio.to_thread(
            send_message, settings.telegram_bot_token, settings.telegram_chat_id, "Live Intercom test message."
        )
        return JSONResponse({"ok": ok, "reason": reason})

    async def ws_endpoint(ws: WebSocket) -> None:
        if not origin_allowed(ws) or current_user(ws) is None:
            await ws.close(code=1008)
            return
        await manager.handle(ws)

    routes: list = [
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/api/me", me, methods=["GET"]),
        Route("/api/admin/settings", get_admin_settings, methods=["GET"]),
        Route("/api/admin/settings", save_admin_settings, methods=["POST"]),
        Route("/api/admin/telegram/test", test_telegram, methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
    ]
    if config.static_dir.is_dir():
        routes.append(Mount("/", StaticFiles(directory=config.static_dir, html=True)))
    return Starlette(routes=routes)
