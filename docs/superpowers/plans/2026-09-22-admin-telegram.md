# Admin Page + Telegram Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any logged-in user can open a Settings view, save a Telegram bot token and group chat ID (persisted in a new admin-editable `settings.toml`), and send a test message to confirm they work.

**Architecture:** Two new backend modules (`settings.py` for persisted config, `telegram.py` for the Bot API call) plumbed into three new `web.py` routes behind the existing session-cookie auth, plus a new admin view in the existing single-page vanilla-TS frontend (no router, same `el()`/`show*()` pattern as the login and intercom views).

**Tech Stack:** Python 3.11+/Starlette (backend), stdlib `tomllib`/`urllib.request` (no new runtime dependencies), Vite/TypeScript/vitest (frontend, no framework).

**Spec:** [`docs/superpowers/specs/2026-09-22-admin-telegram-design.md`](../specs/2026-09-22-admin-telegram-design.md)

## Global Constraints

- No new runtime dependencies — Telegram calls use `urllib.request`, not `httpx` (spec: "HTTP client for Telegram's Bot API").
- Any logged-in user may reach the admin endpoints/page — no new role (spec: "Who can reach the admin page").
- Settings files are written atomically (tmp file + `os.replace`) with `0600` permissions, matching `users.toml` (spec: "Settings storage").
- The saved Telegram bot token is never sent back to the browser in full — only a masked suffix (spec: "Token round-tripping").
- Frontend has no DOM test harness today (`framing.test.ts` is logic-only) — the admin view is verified via `npm run build` (strict type check) plus manual browser testing, not new automated UI tests (spec: "Testing").

---

### Task 1: `settings_file` config option

**Files:**
- Modify: `backend/live_intercom/config.py`
- Modify: `backend/tests/test_config.py`
- Modify: `backend/tests/test_web.py:12-27` (`make_config` helper)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Config.settings_file: Path`, resolved the same way as `Config.audio`/`auth.users_file` — later tasks read this to find `settings.toml`.

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_config.py`, add an assertion to `test_defaults` and a new override test:

```python
def test_defaults(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("")
    cfg = load_config(path)
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.audio.device_match == "Jabra"
    assert cfg.audio.echo_cancel == "off"
    assert cfg.audio.jitter_ms == 60
    assert cfg.auth.session_hours == 12
    assert cfg.auth.secure_cookie is False
    assert cfg.session.idle_timeout_s == 10.0
    assert cfg.auth.users_file == tmp_path / "users.toml"
    assert cfg.settings_file == tmp_path / "settings.toml"


def test_settings_file_override(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('settings_file = "/etc/settings.toml"\n')
    cfg = load_config(path)
    assert cfg.settings_file == Path("/etc/settings.toml")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `TypeError: Config.__init__() missing 1 required positional argument: 'settings_file'` (or `AttributeError: 'Config' object has no attribute 'settings_file'`).

- [ ] **Step 3: Add `settings_file` to `Config` and resolve it in `load_config`**

In `backend/live_intercom/config.py`, add the field to the `Config` dataclass (after `static_dir`):

```python
@dataclass(frozen=True)
class Config:
    host: str
    port: int
    static_dir: Path
    settings_file: Path
    audio: AudioConfig
    auth: AuthConfig
    session: SessionConfig
```

And resolve it in `load_config`, right after `static_dir`:

```python
    return Config(
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 8000)),
        static_dir=resolve(raw.get("static_dir"), "static"),
        settings_file=resolve(raw.get("settings_file"), "settings.toml"),
        audio=audio,
        auth=AuthConfig(
            users_file=resolve(auth_raw.get("users_file"), "users.toml"),
            secret_file=resolve(auth_raw.get("secret_file"), "secret.key"),
            session_hours=float(auth_raw.get("session_hours", 12)),
            secure_cookie=bool(auth_raw.get("secure_cookie", False)),
        ),
        session=SessionConfig(idle_timeout_s=float(session_raw.get("idle_timeout_s", 10.0))),
    )
```

- [ ] **Step 4: Update `make_config` in `test_web.py` so the whole suite keeps constructing `Config`**

In `backend/tests/test_web.py`, `make_config` currently reads:

```python
    return Config(
        host="127.0.0.1",
        port=8000,
        static_dir=tmp_path / "static",
        audio=AudioConfig(),
        auth=AuthConfig(
            users_file=users,
            secret_file=tmp_path / "secret.key",
            session_hours=12,
            secure_cookie=secure_cookie,
        ),
        session=SessionConfig(idle_timeout_s=5.0),
    )
```

Add `settings_file` after `static_dir`:

```python
    return Config(
        host="127.0.0.1",
        port=8000,
        static_dir=tmp_path / "static",
        settings_file=tmp_path / "settings.toml",
        audio=AudioConfig(),
        auth=AuthConfig(
            users_file=users,
            secret_file=tmp_path / "secret.key",
            session_hours=12,
            secure_cookie=secure_cookie,
        ),
        session=SessionConfig(idle_timeout_s=5.0),
    )
```

- [ ] **Step 5: Run the full backend test suite to verify it passes**

Run: `cd backend && .venv/bin/pytest -q`
Expected: PASS, all tests (including `test_web.py`) green.

- [ ] **Step 6: Commit**

```bash
git add backend/live_intercom/config.py backend/tests/test_config.py backend/tests/test_web.py
git commit -m "feat: add settings_file config option

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: `settings.py` — persisted Telegram settings

**Files:**
- Create: `backend/live_intercom/settings.py`
- Test: `backend/tests/test_settings.py`

**Interfaces:**
- Consumes: `Config.settings_file` (Task 1).
- Produces: `Settings` dataclass (`telegram_bot_token: str = ""`, `telegram_chat_id: str = ""`); `load_settings(path: Path) -> Settings`; `save_settings(path: Path, settings: Settings) -> None` (raises `ValueError("invalid_chat_id")` / `ValueError("invalid_token")`); `mask_token(token: str) -> str`. Task 4 (`web.py`) imports all four names.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_settings.py`:

```python
import stat

import pytest

from live_intercom.settings import Settings, load_settings, mask_token, save_settings


def test_load_missing_file_returns_defaults(tmp_path):
    assert load_settings(tmp_path / "settings.toml") == Settings()


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.toml"
    save_settings(path, Settings(telegram_bot_token="123456:ABCdef", telegram_chat_id="-1001234567890"))
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "123456:ABCdef"
    assert loaded.telegram_chat_id == "-1001234567890"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_overwrites_previous_values(tmp_path):
    path = tmp_path / "settings.toml"
    save_settings(path, Settings(telegram_bot_token="111:aaa", telegram_chat_id="-1"))
    save_settings(path, Settings(telegram_bot_token="222:bbb", telegram_chat_id="-2"))
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "222:bbb"
    assert loaded.telegram_chat_id == "-2"


@pytest.mark.parametrize("chat_id", ["", "not-a-number", "12.5"])
def test_save_rejects_bad_chat_id(tmp_path, chat_id):
    with pytest.raises(ValueError, match="invalid_chat_id"):
        save_settings(tmp_path / "settings.toml", Settings(telegram_bot_token="123:abc", telegram_chat_id=chat_id))


@pytest.mark.parametrize("token", ["", "no-colon", "abc:def", "123:"])
def test_save_rejects_bad_token(tmp_path, token):
    with pytest.raises(ValueError, match="invalid_token"):
        save_settings(tmp_path / "settings.toml", Settings(telegram_bot_token=token, telegram_chat_id="-1001"))


def test_mask_token():
    assert mask_token("") == ""
    assert mask_token("123456:ABCDEFGH") == "••••EFGH"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_intercom.settings'`.

- [ ] **Step 3: Implement `settings.py`**

Create `backend/live_intercom/settings.py`:

```python
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CHAT_ID_RE = re.compile(r"^-?\d+$")
TOKEN_RE = re.compile(r"^\d+:\S+$")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def load_settings(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    raw = tomllib.loads(path.read_text()).get("telegram", {})
    return Settings(
        telegram_bot_token=str(raw.get("bot_token", "")),
        telegram_chat_id=str(raw.get("chat_id", "")),
    )


def save_settings(path: Path, settings: Settings) -> None:
    if not CHAT_ID_RE.match(settings.telegram_chat_id):
        raise ValueError("invalid_chat_id")
    if not TOKEN_RE.match(settings.telegram_bot_token):
        raise ValueError("invalid_token")
    lines = [
        "[telegram]",
        f"bot_token = {json.dumps(settings.telegram_bot_token)}",
        f"chat_id = {json.dumps(settings.telegram_chat_id)}",
    ]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def mask_token(token: str) -> str:
    if not token:
        return ""
    return "••••" + token[-4:]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_settings.py -v`
Expected: PASS, all 8 tests (2 base + 3 parametrized chat-id + 4 parametrized token, per `pytest -v` expansion) green.

- [ ] **Step 5: Commit**

```bash
git add backend/live_intercom/settings.py backend/tests/test_settings.py
git commit -m "feat: add settings.py for persisted Telegram config

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: `telegram.py` — send a message via the Bot API

**Files:**
- Create: `backend/live_intercom/telegram.py`
- Test: `backend/tests/test_telegram.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure `(token, chat_id, text)` in, no dependency on `Settings`).
- Produces: `send_message(token: str, chat_id: str, text: str) -> tuple[bool, str]` — `(True, "")` on success, `(False, reason)` on any failure. Task 4 (`web.py`) imports `send_message` and runs it via `asyncio.to_thread`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_telegram.py`:

```python
import json
from io import BytesIO
from urllib.error import HTTPError, URLError

from live_intercom import telegram


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def test_send_message_success(monkeypatch):
    monkeypatch.setattr(telegram, "urlopen", lambda request, timeout=10: FakeResponse({"ok": True}))
    ok, reason = telegram.send_message("123456:abc", "-1001", "hi")
    assert ok is True
    assert reason == ""


def test_send_message_ok_false_in_200_response(monkeypatch):
    monkeypatch.setattr(
        telegram, "urlopen", lambda request, timeout=10: FakeResponse({"ok": False, "description": "chat not found"})
    )
    ok, reason = telegram.send_message("123456:abc", "-1001", "hi")
    assert ok is False
    assert reason == "chat not found"


def test_send_message_telegram_http_error(monkeypatch):
    body = json.dumps({"ok": False, "description": "Unauthorized"}).encode()

    def boom(request: object, timeout: float = 10) -> None:
        raise HTTPError("url", 401, "Unauthorized", {}, BytesIO(body))

    monkeypatch.setattr(telegram, "urlopen", boom)
    ok, reason = telegram.send_message("bad:token", "-1001", "hi")
    assert ok is False
    assert reason == "Unauthorized"


def test_send_message_network_error(monkeypatch):
    def boom(request: object, timeout: float = 10) -> None:
        raise URLError("no route to host")

    monkeypatch.setattr(telegram, "urlopen", boom)
    ok, reason = telegram.send_message("123456:abc", "-1001", "hi")
    assert ok is False
    assert reason == "network_error"


def test_send_message_sends_expected_payload(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return FakeResponse({"ok": True})

    monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
    telegram.send_message("123456:abc", "-1001", "hello there")
    assert captured["url"] == "https://api.telegram.org/bot123456:abc/sendMessage"
    assert captured["body"] == {"chat_id": "-1001", "text": "hello there"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_telegram.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_intercom.telegram'`.

- [ ] **Step 3: Implement `telegram.py`**

Create `backend/live_intercom/telegram.py`:

```python
from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TIMEOUT_S = 10


def send_message(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """Send a Telegram message. Never raises: every failure comes back as (False, reason)."""
    url = f"{API_BASE}/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    request = Request(url, data=payload, headers={"content-type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=TIMEOUT_S) as response:
            body = json.loads(response.read())
    except HTTPError as exc:
        try:
            body = json.loads(exc.read())
            return False, str(body.get("description", "telegram_error"))[:200]
        except (ValueError, UnicodeDecodeError):
            return False, "telegram_error"
    except (URLError, TimeoutError, OSError):
        log.warning("telegram send_message network error", exc_info=True)
        return False, "network_error"
    except ValueError:
        return False, "telegram_error"
    if not body.get("ok", False):
        return False, str(body.get("description", "telegram_error"))[:200]
    return True, ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_telegram.py -v`
Expected: PASS, all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add backend/live_intercom/telegram.py backend/tests/test_telegram.py
git commit -m "feat: add telegram.py to send messages via the Bot API

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Admin settings routes in `web.py`

**Files:**
- Modify: `backend/live_intercom/web.py`
- Modify: `backend/tests/test_web.py`

**Interfaces:**
- Consumes: `Config.settings_file` (Task 1); `Settings`, `load_settings`, `save_settings`, `mask_token` (Task 2); `send_message` (Task 3); existing `current_user(conn) -> str | None` closure already defined in `create_app`.
- Produces: `GET /api/admin/settings` → `{chat_id, token_masked, token_set}`; `POST /api/admin/settings` (body `{chat_id, token}`) → `{ok: true}` or `400 {error}`; `POST /api/admin/telegram/test` → `{ok, reason}`. Task 5 (frontend) calls these three routes.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_web.py`, add near the top of the imports:

```python
from live_intercom.settings import Settings, save_settings
```

Then append these tests at the end of the file:

```python
def test_admin_settings_requires_login(client):
    assert client.get("/api/admin/settings").status_code == 401
    assert client.post("/api/admin/settings", json={"chat_id": "-1", "token": "1:a"}).status_code == 401
    assert client.post("/api/admin/telegram/test").status_code == 401


def test_admin_settings_defaults_when_unset(client):
    login(client)
    body = client.get("/api/admin/settings").json()
    assert body == {"chat_id": "", "token_masked": "", "token_set": False}


def test_admin_settings_save_and_roundtrip(client):
    login(client)
    response = client.post(
        "/api/admin/settings", json={"chat_id": "-1001234567890", "token": "123456:ABCDEFGH"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    body = client.get("/api/admin/settings").json()
    assert body == {"chat_id": "-1001234567890", "token_masked": "••••EFGH", "token_set": True}


def test_admin_settings_keeps_token_when_masked_value_resubmitted(client):
    login(client)
    client.post("/api/admin/settings", json={"chat_id": "-1", "token": "123456:ABCDEFGH"})
    masked = client.get("/api/admin/settings").json()["token_masked"]
    response = client.post("/api/admin/settings", json={"chat_id": "-2", "token": masked})
    assert response.status_code == 200
    body = client.get("/api/admin/settings").json()
    assert body == {"chat_id": "-2", "token_masked": "••••EFGH", "token_set": True}


def test_admin_settings_rejects_invalid_chat_id(client):
    login(client)
    response = client.post("/api/admin/settings", json={"chat_id": "not-a-number", "token": "123456:ABCDEFGH"})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_chat_id"}


def test_admin_settings_rejects_invalid_token(client):
    login(client)
    response = client.post("/api/admin/settings", json={"chat_id": "-1", "token": "not-a-token"})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_token"}


def test_admin_settings_rejects_bad_body(client):
    login(client)
    assert client.post("/api/admin/settings", content=b"not json").status_code == 400
    assert client.post("/api/admin/settings", json={"chat_id": "-1"}).status_code == 400


def test_telegram_test_not_configured(client):
    login(client)
    response = client.post("/api/admin/telegram/test")
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "not_configured"}


def test_telegram_test_success(client, tmp_path, monkeypatch):
    login(client)
    save_settings(tmp_path / "settings.toml", Settings(telegram_bot_token="123456:abc", telegram_chat_id="-1"))
    monkeypatch.setattr("live_intercom.web.send_message", lambda token, chat_id, text: (True, ""))
    response = client.post("/api/admin/telegram/test")
    assert response.json() == {"ok": True, "reason": ""}


def test_telegram_test_failure(client, tmp_path, monkeypatch):
    login(client)
    save_settings(tmp_path / "settings.toml", Settings(telegram_bot_token="123456:abc", telegram_chat_id="-1"))
    monkeypatch.setattr("live_intercom.web.send_message", lambda token, chat_id, text: (False, "chat not found"))
    response = client.post("/api/admin/telegram/test")
    assert response.json() == {"ok": False, "reason": "chat not found"}
```

Note: the `client` fixture's `make_config` uses `tmp_path / "settings.toml"` (Task 1), so `save_settings(tmp_path / "settings.toml", ...)` in the last two tests writes to the same path the app reads from.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_web.py -v -k admin_settings or telegram_test`
Expected: FAIL — `404 Not Found` for the new routes (`assert 404 == 401` etc.), since `/api/admin/*` doesn't exist yet.

- [ ] **Step 3: Add the admin routes to `web.py`**

In `backend/live_intercom/web.py`, add `import asyncio` to the top-level imports (it isn't imported yet):

```python
from __future__ import annotations

import asyncio
import logging
from typing import Callable
from urllib.parse import urlsplit
```

Add to the imports from the package:

```python
from .config import Config
from .session import SessionManager
from .settings import Settings, load_settings, mask_token, save_settings
from .telegram import send_message
```

Inside `create_app`, add three new handlers after `me` (and before `ws_endpoint`):

```python
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
```

And register the routes in the `routes` list, after `/api/me`:

```python
    routes: list = [
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/api/me", me, methods=["GET"]),
        Route("/api/admin/settings", get_admin_settings, methods=["GET"]),
        Route("/api/admin/settings", save_admin_settings, methods=["POST"]),
        Route("/api/admin/telegram/test", test_telegram, methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest -q`
Expected: PASS, full suite green (68 previous tests + new settings/telegram/web tests).

- [ ] **Step 5: Commit**

```bash
git add backend/live_intercom/web.py backend/tests/test_web.py
git commit -m "feat: add admin settings and Telegram test-send routes

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Frontend admin settings view

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/main.ts`

**Interfaces:**
- Consumes: `GET /api/admin/settings`, `POST /api/admin/settings`, `POST /api/admin/telegram/test` (Task 4).
- Produces: `showAdmin(username: string): void`, wired from a new "Settings" link in `showIntercom`. Nothing later depends on this (terminal task of this plan).

There is no DOM test harness in this project today (`framing.test.ts` covers pure logic only, no jsdom configured — see Global Constraints), so this task is verified with `npm run build` (strict `tsc --noEmit` type check) after each change, plus a manual check in the browser at the end, instead of a red/green unit-test cycle.

- [ ] **Step 1: Add the admin API functions to `api.ts`**

Append to `frontend/src/api.ts`:

```typescript
export interface AdminSettings {
  chat_id: string;
  token_masked: string;
  token_set: boolean;
}

export async function getAdminSettings(): Promise<AdminSettings> {
  const response = await fetch("api/admin/settings");
  if (!response.ok) throw new Error(`status check failed (${response.status})`);
  return response.json();
}

export type SaveSettingsResult = "ok" | "invalid_chat_id" | "invalid_token" | "error";

export async function saveAdminSettings(chatId: string, token: string): Promise<SaveSettingsResult> {
  const response = await fetch("api/admin/settings", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, token }),
  });
  if (response.ok) return "ok";
  const body = await response.json().catch(() => ({}));
  return body.error === "invalid_chat_id" || body.error === "invalid_token" ? body.error : "error";
}

export interface TelegramTestResult {
  ok: boolean;
  reason?: string;
}

export async function sendTelegramTest(): Promise<TelegramTestResult> {
  const response = await fetch("api/admin/telegram/test", { method: "POST" });
  if (!response.ok) return { ok: false, reason: "error" };
  return response.json();
}
```

- [ ] **Step 2: Run the build to verify it still type-checks**

Run: `cd frontend && npm run build`
Expected: PASS (no type errors) — these are new exports, nothing consumes them yet.

- [ ] **Step 3: Add `showAdmin` and the "Settings" link to `main.ts`**

In `frontend/src/main.ts`, update the import line:

```typescript
import { getMe, login, logout, getAdminSettings, saveAdminSettings, sendTelegramTest } from "./api";
```

Add a new `showAdmin` function, placed after `showIntercom`:

```typescript
function showAdmin(username: string): void {
  const chatId = el("input", { type: "text", placeholder: "Chat ID (e.g. -1001234567890)" });
  const token = el("input", { type: "text", placeholder: "Bot token" });
  const status = el("p", { className: "status" });
  const testButton = el("button", { type: "button", textContent: "Send test message" });
  const back = el("button", { className: "link", textContent: "Back" });
  const form = el(
    "form",
    {},
    el("h1", { textContent: "Settings" }),
    chatId,
    token,
    el("button", { textContent: "Save" }),
    testButton,
    status,
    back,
  );

  void getAdminSettings().then((settings) => {
    chatId.value = settings.chat_id;
    token.value = settings.token_set ? settings.token_masked : "";
  });

  form.onsubmit = async (event) => {
    event.preventDefault();
    status.textContent = "Saving…";
    const result = await saveAdminSettings(chatId.value, token.value).catch(() => "error" as const);
    status.textContent =
      result === "ok" ? "Saved." :
      result === "invalid_chat_id" ? "Chat ID looks wrong." :
      result === "invalid_token" ? "Bot token looks wrong." :
      "Could not save settings.";
  };
  testButton.onclick = async () => {
    status.textContent = "Sending…";
    const result = await sendTelegramTest().catch(() => ({ ok: false, reason: "error" }) as const);
    status.textContent = result.ok ? "Sent." : `Failed: ${result.reason ?? "error"}`;
  };
  back.onclick = () => void start();

  app.replaceChildren(form);
}
```

`username` is accepted but unused for now (kept for signature symmetry with `showIntercom` and in case a future "signed in as" line is added). `tsconfig.json` sets `noUnusedLocals` but not `noUnusedParameters`, so this doesn't fail the build.

Update `showIntercom` to add the Settings link next to Sign out:

```typescript
function showIntercom(username: string, audioAvailable: boolean): void {
  const button = el("button", { className: "mic", textContent: LABELS.idle });
  const status = el("p", { className: "status", textContent: audioAvailable ? "" : "Audio device unavailable." });
  const settingsLink = el("button", { className: "link", textContent: "Settings" });
  const signOut = el("button", { className: "link", textContent: `Sign out ${username}` });
  button.disabled = !audioAvailable;

  let running = false;
  const intercom = new Intercom((state, detail) => {
    running = state === "live" || state === "connecting";
    button.textContent = LABELS[state];
    button.dataset.state = state;
    button.disabled = state === "connecting";
    status.textContent = detail ?? (state === "live" ? "You are connected to the room." : "");
  });
  button.onclick = () => {
    if (running) intercom.stop();
    else void intercom.start();
  };
  settingsLink.onclick = () => showAdmin(username);
  signOut.onclick = async () => { intercom.stop(); await logout(); showLogin(); };
  app.replaceChildren(el("h1", { textContent: "Live Intercom" }), button, status, settingsLink, signOut);
}
```

- [ ] **Step 4: Run the build to verify it type-checks**

Run: `cd frontend && npm run build`
Expected: PASS.

- [ ] **Step 5: Run the full frontend test suite**

Run: `cd frontend && npm test`
Expected: PASS, existing 8 tests unaffected (this task adds no new test files, per Global Constraints).

- [ ] **Step 6: Manual verification**

Run: `cd backend && .venv/bin/python -m live_intercom --config config.dev.toml serve` and `cd frontend && npm run dev` in a second terminal. In the browser: log in, click "Settings", confirm the form loads (empty fields on first run), enter a chat ID and a token in `NNNN:xxxx` shape, Save, reload the page and confirm the token field shows a masked value and the chat ID persisted, then click "Send test message" and confirm it reports "Failed: ..." for a fake token (no real bot needed to verify the wiring — a real token/chat ID from a Telegram bot you control confirms end-to-end delivery).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api.ts frontend/src/main.ts
git commit -m "feat: add admin settings view to the frontend

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Post-plan

Once this plan is done, `docs/superpowers/specs/2026-09-22-admin-telegram-design.md`'s "Out of scope" section becomes the seed for sub-project 2 (phone-like ring/confirm flow, using the `settings.toml` foundation built here for its auto-pickup toggle) and sub-project 3 (host-initiated proactive call, using `telegram.send_message` built here). Each gets its own brainstorming → spec → plan cycle before implementation starts.
