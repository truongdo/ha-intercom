from __future__ import annotations

from typing import Callable

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

COOKIE = "intercom_session"
LOCAL_PEERS = ("127.0.0.1", "::1")


def client_ip(conn: HTTPConnection) -> str:
    peer = conn.client.host if conn.client else ""
    forwarded = conn.headers.get("cf-connecting-ip")
    # Only trust the tunnel's header when the request came from the local cloudflared.
    if peer in LOCAL_PEERS and forwarded:
        return forwarded
    return peer


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
        users = load_users(config.auth.users_file)
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
        return JSONResponse(
            {"username": user, "audio_available": audio_probe(not manager.active)}
        )

    async def ws_endpoint(ws: WebSocket) -> None:
        if current_user(ws) is None:
            await ws.close(code=1008)
            return
        await manager.handle(ws)

    routes: list = [
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/api/me", me, methods=["GET"]),
        WebSocketRoute("/ws", ws_endpoint),
    ]
    if config.static_dir.is_dir():
        routes.append(Mount("/", StaticFiles(directory=config.static_dir, html=True)))
    return Starlette(routes=routes)
