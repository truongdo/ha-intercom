from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import replace
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
from .settings import (
    Settings,
    generate_call_confirm_token,
    load_settings,
    mask_token,
    save_call_confirm_token,
    save_call_settings,
    save_telegram_settings,
)
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

    def get_pickup_mode() -> str:
        try:
            return load_settings(config.settings_file).pickup_mode
        except (OSError, ValueError):
            log.exception(
                "cannot read settings file %s for pickup_mode; defaulting to auto",
                config.settings_file,
            )
            return "auto"

    manager = SessionManager(
        device_factory,
        config.audio.jitter_ms,
        config.session.idle_timeout_s,
        pickup_mode_provider=get_pickup_mode,
        ring_timeout_s=config.session.ring_timeout_s,
    )

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

    async def load_admin_settings() -> Settings | Response:
        # tomllib.TOMLDecodeError (a ValueError subclass) on a corrupted settings.toml, or OSError
        # on an unreadable file, both surface the same way login() handles an unreadable users file.
        try:
            return await run_in_threadpool(load_settings, config.settings_file)
        except (OSError, ValueError):
            log.exception("cannot read settings file %s", config.settings_file)
            return JSONResponse({"error": "settings_unavailable"}, status_code=503)

    async def get_admin_settings(request: Request) -> Response:
        if current_user(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        settings = await load_admin_settings()
        if isinstance(settings, Response):
            return settings
        if not settings.call_confirm_token:
            token = generate_call_confirm_token()
            try:
                await run_in_threadpool(save_call_confirm_token, config.settings_file, token)
            except OSError:
                log.exception("cannot write settings file %s", config.settings_file)
            else:
                settings = replace(settings, call_confirm_token=token)
        return JSONResponse(
            {
                "chat_id": settings.telegram_chat_id,
                "token_masked": mask_token(settings.telegram_bot_token),
                "token_set": bool(settings.telegram_bot_token),
                "pickup_mode": settings.pickup_mode,
                "call_confirm_token": settings.call_confirm_token,
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
        current = await load_admin_settings()
        if isinstance(current, Response):
            return current
        if current.telegram_bot_token and token == mask_token(current.telegram_bot_token):
            token = current.telegram_bot_token
        try:
            await run_in_threadpool(save_telegram_settings, config.settings_file, token, chat_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError:
            log.exception("cannot write settings file %s", config.settings_file)
            return JSONResponse({"error": "settings_unavailable"}, status_code=503)
        return JSONResponse({"ok": True})

    async def save_call_settings_route(request: Request) -> Response:
        if current_user(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            pickup_mode = str(body["pickup_mode"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        try:
            await run_in_threadpool(save_call_settings, config.settings_file, pickup_mode)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError:
            log.exception("cannot write settings file %s", config.settings_file)
            return JSONResponse({"error": "settings_unavailable"}, status_code=503)
        return JSONResponse({"ok": True})

    async def regenerate_call_token(request: Request) -> Response:
        if current_user(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        token = generate_call_confirm_token()
        try:
            await run_in_threadpool(save_call_confirm_token, config.settings_file, token)
        except OSError:
            log.exception("cannot write settings file %s", config.settings_file)
            return JSONResponse({"error": "settings_unavailable"}, status_code=503)
        return JSONResponse({"call_confirm_token": token})

    async def test_telegram(request: Request) -> Response:
        if current_user(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        settings = await load_admin_settings()
        if isinstance(settings, Response):
            return settings
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return JSONResponse({"ok": False, "reason": "not_configured"})
        ok, reason = await asyncio.to_thread(
            send_message, settings.telegram_bot_token, settings.telegram_chat_id, "Live Intercom test message."
        )
        return JSONResponse({"ok": ok, "reason": reason})

    async def handle_call_decision(request: Request, act: Callable[[], bool]) -> Response:
        settings = await load_admin_settings()
        if isinstance(settings, Response):
            return settings
        supplied = request.query_params.get("token", "").encode()
        configured = settings.call_confirm_token.encode()
        if not configured or not hmac.compare_digest(supplied, configured):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if act():
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "reason": "no_pending_call"})

    async def call_confirm(request: Request) -> Response:
        return await handle_call_decision(request, manager.confirm)

    async def call_reject(request: Request) -> Response:
        return await handle_call_decision(request, manager.reject)

    async def call_trigger(request: Request) -> Response:
        settings = await load_admin_settings()
        if isinstance(settings, Response):
            return settings
        supplied = request.query_params.get("token", "").encode()
        configured = settings.call_confirm_token.encode()
        if not configured or not hmac.compare_digest(supplied, configured):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        manager.trigger_bypass()
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return JSONResponse({"ok": False, "reason": "not_configured"})
        text = (
            f"📞 Someone wants to talk — open the intercom to answer: {config.public_url}"
            if config.public_url
            else "📞 Someone wants to talk — open the intercom to answer."
        )
        ok, reason = await asyncio.to_thread(
            send_message, settings.telegram_bot_token, settings.telegram_chat_id, text
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
        Route("/api/admin/call-settings", save_call_settings_route, methods=["POST"]),
        Route("/api/admin/call-token/regenerate", regenerate_call_token, methods=["POST"]),
        Route("/api/call/confirm", call_confirm, methods=["GET"]),
        Route("/api/call/reject", call_reject, methods=["GET"]),
        Route("/api/call/trigger", call_trigger, methods=["GET"]),
        WebSocketRoute("/ws", ws_endpoint),
    ]
    if config.static_dir.is_dir():
        routes.append(Mount("/", StaticFiles(directory=config.static_dir, html=True)))
    return Starlette(routes=routes)
