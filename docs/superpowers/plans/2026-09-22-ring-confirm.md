# Phone-Like Ring/Confirm Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `pickup_mode` is `"confirm"`, a remote client's tap rings the host (a looping tone through the Jabra speaker) instead of connecting immediately, and waits for a Home Assistant REST call to confirm or reject before the call goes live. `"auto"` mode (today's behavior) is unchanged. Configurable via the admin page.

**Architecture:** `SessionManager._run()` opens the audio device once per call attempt and, in confirm mode, routes a synthesized ringtone through it while racing a confirm event against a timeout and the existing client-disconnect/stop path; on confirm it swaps the same open stream over to real audio with no teardown. Two new unauthenticated-by-cookie HTTP routes (`GET /api/call/confirm`/`reject`), gated by a shared-secret token, resolve the pending call.

**Tech Stack:** Same as sub-project 1 — Python 3.11+/Starlette, stdlib only (`numpy` already a dependency, used here for tone synthesis), Vite/TypeScript/vitest.

**Spec:** [`docs/superpowers/specs/2026-09-22-ring-confirm-design.md`](../specs/2026-09-22-ring-confirm-design.md)

## Global Constraints

- No new runtime dependencies (spec: reuses `numpy`, already a dependency).
- Confirm/reject auth is a shared-secret token in the URL query string, checked with `hmac.compare_digest`, and always `401` if no token is configured — never an implicit empty-matches-empty bypass (spec: "Confirm/reject auth").
- `ring_timeout_s` lives only in `config.toml` (`[session]`), never admin-page-editable (spec: "Ring timeout location").
- `pickup_mode`/`call_confirm_token` persist independently from the Telegram fields in the same `settings.toml`, so saving one never requires the other to be valid (spec: "Settings persistence").
- Ring→live handoff is one continuous PortAudio stream per call attempt — the frame source and mic sink are swapped via indirection, never a stream teardown/reopen (spec: "Ring→live audio handoff").
- Home Assistant is never proactively notified of a ringing call — the ringtone is the only notification (spec: "Does HA get pushed a ringing notification").
- Frontend has no DOM test harness (carried over from sub-project 1) — the admin UI additions are verified via `npm run build` plus manual testing, not new automated UI tests.
- Every task's commit leaves the full test suite green — `SessionManager`'s constructor and `web.py`'s only call site of it change together in the same task (see Task 3), not across a task boundary.

---

### Task 1: Extend `settings.py` for call settings; section Telegram/call persistence independently

**Files:**
- Modify: `backend/live_intercom/settings.py`
- Modify: `backend/live_intercom/web.py:26` (import), `backend/live_intercom/web.py:148-153` (`save_admin_settings`'s save call)
- Modify: `backend/tests/test_settings.py` (full rewrite — see Step 1)
- Modify: `backend/tests/test_web.py:8` (import), `:232`, `:240` (`test_telegram_test_success`/`_failure`), `:266-275` (`test_admin_settings_save_returns_503_when_write_fails`)

**Interfaces:**
- Consumes: nothing from later tasks.
- Produces: `Settings` gains `pickup_mode: str = "auto"`, `call_confirm_token: str = ""`. `save_settings` is replaced by `save_telegram_settings(path: Path, token: str, chat_id: str) -> None` (same validation/atomic-write/0600 guarantees, new argument order: token before chat_id). New: `save_call_settings(path: Path, pickup_mode: str) -> None` (raises `ValueError("invalid_pickup_mode")`), `save_call_confirm_token(path: Path, token: str) -> None`, `generate_call_confirm_token() -> str`. `load_settings` reads both sections. Task 3 (config wiring is in Task 3, settings.py's `save_call_confirm_token` also used there) and Task 4 import these.

Today's `save_settings` blindly overwrites the whole file with only the `[telegram]` section — fine when Telegram was the only concern, but wrong once a `[call]` section needs to coexist. This task changes persistence to read-modify-write, touching only the section being saved.

- [ ] **Step 1: Write the failing tests**

Replace `backend/tests/test_settings.py` entirely with:

```python
import stat

import pytest

from live_intercom.settings import (
    Settings,
    generate_call_confirm_token,
    load_settings,
    mask_token,
    save_call_confirm_token,
    save_call_settings,
    save_telegram_settings,
)


def test_load_missing_file_returns_defaults(tmp_path):
    assert load_settings(tmp_path / "settings.toml") == Settings()


def test_save_telegram_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.toml"
    save_telegram_settings(path, "123456:ABCdef", "-1001234567890")
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "123456:ABCdef"
    assert loaded.telegram_chat_id == "-1001234567890"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_telegram_overwrites_previous_values(tmp_path):
    path = tmp_path / "settings.toml"
    save_telegram_settings(path, "111:aaa", "-1")
    save_telegram_settings(path, "222:bbb", "-2")
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "222:bbb"
    assert loaded.telegram_chat_id == "-2"


@pytest.mark.parametrize("chat_id", ["", "not-a-number", "12.5"])
def test_save_telegram_rejects_bad_chat_id(tmp_path, chat_id):
    with pytest.raises(ValueError, match="invalid_chat_id"):
        save_telegram_settings(tmp_path / "settings.toml", "123:abc", chat_id)


@pytest.mark.parametrize("token", ["", "no-colon", "abc:def", "123:"])
def test_save_telegram_rejects_bad_token(tmp_path, token):
    with pytest.raises(ValueError, match="invalid_token"):
        save_telegram_settings(tmp_path / "settings.toml", token, "-1001")


def test_mask_token():
    assert mask_token("") == ""
    assert mask_token("123456:ABCDEFGH") == "••••EFGH"


def test_save_call_settings_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.toml"
    save_call_settings(path, "confirm")
    assert load_settings(path).pickup_mode == "confirm"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", ["", "Auto", "always", "off"])
def test_save_call_settings_rejects_bad_mode(tmp_path, mode):
    with pytest.raises(ValueError, match="invalid_pickup_mode"):
        save_call_settings(tmp_path / "settings.toml", mode)


def test_save_call_confirm_token_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.toml"
    save_call_confirm_token(path, "test-token-value")
    assert load_settings(path).call_confirm_token == "test-token-value"


def test_generate_call_confirm_token_is_random_and_long():
    a, b = generate_call_confirm_token(), generate_call_confirm_token()
    assert a != b
    assert len(a) > 20


def test_telegram_and_call_sections_save_independently(tmp_path):
    path = tmp_path / "settings.toml"
    save_telegram_settings(path, "123456:abc", "-1")
    save_call_settings(path, "confirm")
    save_call_confirm_token(path, "known-token")
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "123456:abc"
    assert loaded.telegram_chat_id == "-1"
    assert loaded.pickup_mode == "confirm"
    assert loaded.call_confirm_token == "known-token"

    save_telegram_settings(path, "999999:zzz", "-2")
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "999999:zzz"
    assert loaded.telegram_chat_id == "-2"
    assert loaded.pickup_mode == "confirm"  # untouched by the telegram-only save
    assert loaded.call_confirm_token == "known-token"  # untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_settings.py -v`
Expected: FAIL — `ImportError: cannot import name 'save_telegram_settings'` (and others).

- [ ] **Step 3: Rewrite `settings.py`**

Replace `backend/live_intercom/settings.py` entirely with:

```python
from __future__ import annotations

import json
import os
import re
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path

CHAT_ID_RE = re.compile(r"^-?\d+$")
TOKEN_RE = re.compile(r"^\d+:\S+$")
PICKUP_MODES = ("auto", "confirm")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    pickup_mode: str = "auto"
    call_confirm_token: str = ""


def load_settings(path: Path) -> Settings:
    raw = _load_raw(path)
    telegram = raw.get("telegram", {})
    call = raw.get("call", {})
    return Settings(
        telegram_bot_token=str(telegram.get("bot_token", "")),
        telegram_chat_id=str(telegram.get("chat_id", "")),
        pickup_mode=str(call.get("pickup_mode", "auto")),
        call_confirm_token=str(call.get("call_confirm_token", "")),
    )


def save_telegram_settings(path: Path, token: str, chat_id: str) -> None:
    if not CHAT_ID_RE.match(chat_id):
        raise ValueError("invalid_chat_id")
    if not TOKEN_RE.match(token):
        raise ValueError("invalid_token")
    raw = _load_raw(path)
    raw["telegram"] = {"bot_token": token, "chat_id": chat_id}
    _write_raw(path, raw)


def save_call_settings(path: Path, pickup_mode: str) -> None:
    if pickup_mode not in PICKUP_MODES:
        raise ValueError("invalid_pickup_mode")
    raw = _load_raw(path)
    call = dict(raw.get("call", {}))
    call["pickup_mode"] = pickup_mode
    raw["call"] = call
    _write_raw(path, raw)


def save_call_confirm_token(path: Path, token: str) -> None:
    raw = _load_raw(path)
    call = dict(raw.get("call", {}))
    call["call_confirm_token"] = token
    raw["call"] = call
    _write_raw(path, raw)


def generate_call_confirm_token() -> str:
    return secrets.token_urlsafe(24)


def mask_token(token: str) -> str:
    if not token:
        return ""
    return "••••" + token[-4:]


def _load_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def _write_raw(path: Path, raw: dict) -> None:
    # Only ever write the two sections this module understands, in a fixed order — not a
    # generic re-serializer, so a hand-edited file with a stray unknown section is left as-is
    # in memory but not blindly round-tripped (that section is simply dropped on next save).
    lines = []
    for section in ("telegram", "call"):
        fields = raw.get(section)
        if not fields:
            continue
        lines.append(f"[{section}]")
        for key, value in fields.items():
            lines.append(f"{key} = {json.dumps(value)}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
```

- [ ] **Step 4: Fix up `web.py`'s existing Telegram save call site**

In `backend/live_intercom/web.py`, change the import line:

```python
from .settings import Settings, load_settings, mask_token, save_settings
```

to:

```python
from .settings import load_settings, mask_token, save_telegram_settings
```

(`Settings` the dataclass is no longer constructed in `web.py` — every save function now takes primitive arguments directly.)

In `save_admin_settings`, change:

```python
        try:
            await run_in_threadpool(
                save_settings,
                config.settings_file,
                Settings(telegram_bot_token=token, telegram_chat_id=chat_id),
            )
        except ValueError as exc:
```

to:

```python
        try:
            await run_in_threadpool(save_telegram_settings, config.settings_file, token, chat_id)
        except ValueError as exc:
```

- [ ] **Step 5: Fix up `test_web.py`'s existing references**

Change the import line:

```python
from live_intercom.settings import Settings, save_settings
```

to:

```python
from live_intercom.settings import save_telegram_settings
```

In `test_telegram_test_success` and `test_telegram_test_failure`, change:

```python
    save_settings(tmp_path / "settings.toml", Settings(telegram_bot_token="123456:abc", telegram_chat_id="-1"))
```

to:

```python
    save_telegram_settings(tmp_path / "settings.toml", "123456:abc", "-1")
```

(in both tests — same line appears in each).

In `test_admin_settings_save_returns_503_when_write_fails`, change:

```python
    def boom(path, settings):
        raise OSError("disk full")

    monkeypatch.setattr("live_intercom.web.save_settings", boom)
```

to:

```python
    def boom(path, token, chat_id):
        raise OSError("disk full")

    monkeypatch.setattr("live_intercom.web.save_telegram_settings", boom)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 7: Commit**

```bash
git add backend/live_intercom/settings.py backend/live_intercom/web.py backend/tests/test_settings.py backend/tests/test_web.py
git commit -m "feat: section Telegram/call settings persistence, add call settings fields

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Ringtone audio source

**Files:**
- Create: `backend/live_intercom/audio/ringtone.py`
- Test: `backend/tests/test_ringtone.py`

**Interfaces:**
- Consumes: `FRAME_BYTES`, `RATE` from `protocol.py` (already exist).
- Produces: `RingtoneSource` — instantiate with no arguments, call `.next_frame() -> bytes` repeatedly to get an endless, looping ring cadence, one `FRAME_BYTES`-sized chunk at a time. Also exports `CYCLE_BYTES: int` (the full cadence length, for test/verification use). Task 3 imports `RingtoneSource`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_ringtone.py`:

```python
from live_intercom.audio.ringtone import CYCLE_BYTES, RingtoneSource
from live_intercom.protocol import FRAME_BYTES


def test_frames_are_correct_size():
    source = RingtoneSource()
    for _ in range(500):
        assert len(source.next_frame()) == FRAME_BYTES


def test_cycle_wraps_and_repeats():
    source = RingtoneSource()
    frames_per_cycle = CYCLE_BYTES // FRAME_BYTES
    first_pass = [source.next_frame() for _ in range(frames_per_cycle)]
    second_pass = [source.next_frame() for _ in range(frames_per_cycle)]
    assert first_pass == second_pass


def test_contains_both_tone_and_silence():
    source = RingtoneSource()
    frames = [source.next_frame() for _ in range(CYCLE_BYTES // FRAME_BYTES)]
    silent = bytes(FRAME_BYTES)
    assert any(frame != silent for frame in frames)  # the "on" portion has a tone
    assert any(frame == silent for frame in frames)  # the "off" portion is silent


def test_independent_sources_start_in_sync():
    a, b = RingtoneSource(), RingtoneSource()
    assert a.next_frame() == b.next_frame()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_ringtone.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_intercom.audio.ringtone'`.

- [ ] **Step 3: Implement `ringtone.py`**

Create `backend/live_intercom/audio/ringtone.py`:

```python
from __future__ import annotations

import numpy as np

from ..protocol import FRAME_BYTES, RATE

TONE_HZ = 440.0
RING_ON_S = 1.5
RING_OFF_S = 3.0


def _generate_cycle() -> bytes:
    on_samples = int(RATE * RING_ON_S)
    off_samples = int(RATE * RING_OFF_S)
    t = np.arange(on_samples) / RATE
    tone = (np.sin(2 * np.pi * TONE_HZ * t) * 0.3 * 32767).astype(np.int16)
    silence = np.zeros(off_samples, dtype=np.int16)
    return np.concatenate([tone, silence]).tobytes()


_CYCLE = _generate_cycle()
CYCLE_BYTES = len(_CYCLE)


class RingtoneSource:
    """Cycles through a precomputed ring cadence, one FRAME_BYTES chunk at a time, looping forever."""

    def __init__(self) -> None:
        self._pos = 0

    def next_frame(self) -> bytes:
        end = self._pos + FRAME_BYTES
        if end <= CYCLE_BYTES:
            frame = _CYCLE[self._pos : end]
            self._pos = end % CYCLE_BYTES
        else:
            wrap = end - CYCLE_BYTES
            frame = _CYCLE[self._pos :] + _CYCLE[:wrap]
            self._pos = wrap
        return frame
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_ringtone.py -v`
Expected: PASS, all 4 tests green. (`RING_ON_S=1.5`/`RING_OFF_S=3.0` at `RATE=16000` and `FRAME_BYTES=640` gives a 144000-byte cycle, exactly 225 frames — no partial-frame wraparound in practice, though the wraparound branch is still correct for any future cadence tweak.)

- [ ] **Step 5: Commit**

```bash
git add backend/live_intercom/audio/ringtone.py backend/tests/test_ringtone.py
git commit -m "feat: add ringtone audio source

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Ring/confirm state machine, `ring_timeout_s` config, confirm/reject HTTP routes

**Files:**
- Modify: `backend/live_intercom/protocol.py`
- Modify: `backend/live_intercom/session.py` (substantial restructuring — full new content given below)
- Modify: `backend/live_intercom/config.py`
- Modify: `backend/live_intercom/web.py`
- Modify: `backend/tests/test_protocol.py`
- Modify: `backend/tests/test_session.py` (extend `make_client` helper; existing tests must keep passing unchanged since the helper's new parameters default to today's `"auto"` behavior)
- Modify: `backend/tests/test_config.py`
- Modify: `backend/tests/test_web.py`

**Interfaces:**
- Consumes: `RingtoneSource` (Task 2); `load_settings`, `Settings.call_confirm_token` (Task 1).
- Produces: `protocol.ringing_message() -> dict`, `protocol.rejected_message(reason: str) -> dict`. `SessionManager.__init__` gains two new **required** parameters: `pickup_mode_provider: Callable[[], str]` and `ring_timeout_s: float` (inserted after `idle_timeout_s`). `SessionManager.confirm() -> bool` and `SessionManager.reject() -> bool` — thread-safe. `Config.session.ring_timeout_s: float`. `GET /api/call/confirm?token=...`, `GET /api/call/reject?token=...`. Task 4 (admin call-settings routes) and Task 5 (frontend) build on this task's routes and message shapes.

This task is deliberately larger than usual: `SessionManager`'s constructor and its one production call site in `web.py`'s `create_app` must change together, or the commit leaves `test_web.py` failing (a plan defect caught and fixed during planning — see Global Constraints). **Read Step 4's correctness note before implementing the state machine.**

- [ ] **Step 1: Add the two new protocol messages, with a failing test first**

In `backend/tests/test_protocol.py`, add:

```python
def test_ringing_message():
    assert protocol.ringing_message() == {"type": "ringing"}


def test_rejected_message():
    assert protocol.rejected_message("declined") == {"type": "rejected", "reason": "declined"}
    assert protocol.rejected_message("timeout") == {"type": "rejected", "reason": "timeout"}
```

Run: `cd backend && .venv/bin/pytest tests/test_protocol.py -v`
Expected: FAIL — `AttributeError: module 'live_intercom.protocol' has no attribute 'ringing_message'`.

In `backend/live_intercom/protocol.py`, add after `ready_message`:

```python
def ringing_message() -> dict:
    return {"type": "ringing"}


def rejected_message(reason: str) -> dict:
    return {"type": "rejected", "reason": reason}
```

Run: `cd backend && .venv/bin/pytest tests/test_protocol.py -v`
Expected: PASS.

- [ ] **Step 2: Add `ring_timeout_s` to `config.py`, with a failing test first**

In `backend/tests/test_config.py`, add an assertion to `test_defaults`:

```python
    assert cfg.session.ring_timeout_s == 30.0
```

(add this line alongside the existing `assert cfg.session.idle_timeout_s == 10.0`)

And a new test:

```python
def test_ring_timeout_override(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("[session]\nring_timeout_s = 45\n")
    cfg = load_config(path)
    assert cfg.session.ring_timeout_s == 45.0
```

Run: `cd backend && .venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'SessionConfig' object has no attribute 'ring_timeout_s'`.

In `backend/live_intercom/config.py`, change:

```python
@dataclass(frozen=True)
class SessionConfig:
    idle_timeout_s: float = 10.0
```

to:

```python
@dataclass(frozen=True)
class SessionConfig:
    idle_timeout_s: float = 10.0
    ring_timeout_s: float = 30.0
```

And in `load_config`, change:

```python
        session=SessionConfig(idle_timeout_s=float(session_raw.get("idle_timeout_s", 10.0))),
```

to:

```python
        session=SessionConfig(
            idle_timeout_s=float(session_raw.get("idle_timeout_s", 10.0)),
            ring_timeout_s=float(session_raw.get("ring_timeout_s", 30.0)),
        ),
```

Run: `cd backend && .venv/bin/pytest tests/test_config.py -v`
Expected: PASS. (`test_web.py`'s `make_config` helper constructs `SessionConfig(idle_timeout_s=5.0)` — `ring_timeout_s` has a default, so this needs no change.)

- [ ] **Step 3: Extend the `make_client` test helper in `test_session.py`, write the new session tests (failing)**

In `backend/tests/test_session.py`, change `make_client`:

```python
def make_client(factory, idle=5.0, pickup_mode="auto", ring_timeout=5.0):
    manager = SessionManager(
        factory,
        jitter_ms=60,
        idle_timeout_s=idle,
        pickup_mode_provider=lambda: pickup_mode,
        ring_timeout_s=ring_timeout,
    )

    async def endpoint(ws):
        await manager.handle(ws)

    client = TestClient(Starlette(routes=[WebSocketRoute("/ws", endpoint)]))
    client.manager = manager  # exposed so tests can call confirm()/reject() directly
    return client
```

(All existing tests in this file call `make_client(factory)` or `make_client(factory, idle=X)` — with `pickup_mode` defaulting to `"auto"`, none of them change behavior. Do not modify any existing test body in this file.)

Append these new tests at the end of the file:

```python
def test_confirm_mode_rings_then_confirms_to_live():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == {"type": "ringing"}
            frame = device.pull_speaker()
            assert frame != bytes(len(frame))  # ringtone, not silence
            assert client.manager.confirm() is True
            assert ws.receive_json() == ready_message()
            device.emit_mic(FRAME)
            assert ws.receive_bytes() == FRAME


def test_confirm_mode_reject_ends_session():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            assert client.manager.reject() is True
            assert ws.receive_json() == {"type": "rejected", "reason": "declined"}
        assert wait_for(lambda: device.stopped)


def test_confirm_mode_timeout_rejects():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm", ring_timeout=0.2) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            assert ws.receive_json() == {"type": "rejected", "reason": "timeout"}
        assert wait_for(lambda: device.stopped)


def test_confirm_mode_cancel_while_ringing():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            ws.send_json({"type": "stop"})
        assert wait_for(lambda: device.stopped)


def test_confirm_with_nothing_pending_returns_false():
    with make_client(lambda: FakeDevice(), pickup_mode="auto") as client:
        assert client.manager.confirm() is False
        assert client.manager.reject() is False


def test_double_confirm_second_call_returns_false():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            assert client.manager.confirm() is True
            assert client.manager.confirm() is False  # already resolved
            ws.receive_json()  # ready


def test_idle_timeout_not_triggered_by_long_ring():
    device = FakeDevice()
    # idle_timeout_s (0.3) is shorter than how long we simulate ringing before confirming,
    # proving idle-timeout only starts counting once the call goes live, not from ring-start.
    with make_client(lambda: device, idle=0.3, pickup_mode="confirm", ring_timeout=5.0) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            time.sleep(0.5)
            assert client.manager.confirm() is True
            assert ws.receive_json() == ready_message()  # not an idle_timeout error
```

Run: `cd backend && .venv/bin/pytest tests/test_session.py -v`
Expected: FAIL — `TypeError: SessionManager.__init__() missing 2 required positional arguments` (from the changed `make_client` alone; every test in the file fails at this point, including the ones you didn't touch — expected, since the constructor signature isn't updated yet).

- [ ] **Step 4: The correctness trap — read before implementing**

A naive implementation of the ring/confirm race looks like: "run `asyncio.wait` over `{confirm_task, timeout_task, receive_task}`, then cancel everything in `pending`." **This is wrong.** `receive_task` must survive into the live phase unchanged when the call is confirmed — it's the same task that keeps reading `ws.receive()` for the rest of the call. If confirm/timeout resolves first, `receive_task` is *correctly* still pending — blindly cancelling everything in `pending` cancels it too, and a cancelled task reused in a later `asyncio.wait()` just immediately reports itself "done", silently breaking the live phase's ability to receive audio frames or a "stop" message ever again.

The correct rule: only cancel `receive_task` explicitly, and only once you've decided the call is rejected or timed out (or once `receive_task` itself already finished on its own, meaning the caller cancelled/disconnected). Never cancel it as a side effect of a generic "clean up everything pending" loop.

- [ ] **Step 5: Rewrite `session.py`**

Replace `backend/live_intercom/session.py` entirely with:

```python
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

from starlette.websockets import WebSocket

from .audio.device import AudioDevice, DeviceFactory, DeviceUnavailable
from .audio.jitter import JitterBuffer
from .audio.ringtone import RingtoneSource
from .protocol import FRAME_BYTES, FRAME_MS, ready_message, rejected_message, ringing_message

log = logging.getLogger(__name__)

MIC_QUEUE_FRAMES = 50
STOP_TIMEOUT_S = 3.0


class SessionManager:
    def __init__(
        self,
        device_factory: DeviceFactory,
        jitter_ms: int,
        idle_timeout_s: float,
        pickup_mode_provider: Callable[[], str],
        ring_timeout_s: float,
    ):
        self._device_factory = device_factory
        self._max_frames = max(1, jitter_ms // FRAME_MS)
        self._idle = idle_timeout_s
        self._pickup_mode_provider = pickup_mode_provider
        self._ring_timeout = ring_timeout_s
        self._active = False
        # Serialises device opening with the /api/me PortAudio refresh (see web.py).
        self.device_lock = asyncio.Lock()
        # Set only while a "confirm" mode call is ringing. confirm()/reject() are called from
        # an HTTP handler that may run on a different thread than this session's event loop
        # (true in the test harness; also a robustness guarantee in production), so they
        # resolve the pending event via call_soon_threadsafe rather than touching it directly.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_event: asyncio.Event | None = None
        self._pending_decision: str | None = None

    @property
    def active(self) -> bool:
        return self._active

    def confirm(self) -> bool:
        return self._resolve("confirm")

    def reject(self) -> bool:
        return self._resolve("declined")

    def _resolve(self, decision: str) -> bool:
        if self._pending_event is None or self._loop is None:
            return False
        self._pending_decision = decision
        self._loop.call_soon_threadsafe(self._pending_event.set)
        return True

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        # Single-threaded event loop: no await between check and set, so this is atomic.
        if self._active:
            await _send_json(ws, {"type": "busy"})
            await _close(ws)
            return
        self._active = True
        try:
            await self._run(ws)
        finally:
            self._active = False
            await _close(ws)

    async def _run(self, ws: WebSocket) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        jitter = JitterBuffer(self._max_frames)
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_FRAMES)
        events: asyncio.Queue[str] = asyncio.Queue()
        last_rx = [loop.time()]
        ringtone = RingtoneSource()

        def offer(frame: bytes) -> None:
            if mic_queue.full():
                mic_queue.get_nowait()  # drop oldest: real-time audio prefers a glitch to delay
            mic_queue.put_nowait(frame)

        def on_mic(frame: bytes) -> None:
            loop.call_soon_threadsafe(offer, frame)

        def discard_mic(frame: bytes) -> None:
            pass  # the host's own mic is not routed anywhere while ringing

        def on_error(reason: str) -> None:
            loop.call_soon_threadsafe(events.put_nowait, reason)

        # Indirection so ringing and the live call can share one continuously-open PortAudio
        # stream: device.start() binds these two callables once, but what they point at is
        # swapped in place the instant a call is confirmed — no stream teardown, no pickup delay.
        pull_box: list[Callable[[], bytes]] = [ringtone.next_frame]
        mic_sink_box: list[Callable[[bytes], None]] = [discard_mic]

        def pull_indirect() -> bytes:
            return pull_box[0]()

        def on_mic_indirect(frame: bytes) -> None:
            mic_sink_box[0](frame)

        device: AudioDevice | None = None
        reason: str | None = None
        # PortAudio calls can block for seconds: keep them off the event loop.
        async with self.device_lock:
            try:
                device = await asyncio.to_thread(self._device_factory)
                await asyncio.to_thread(device.start, on_mic_indirect, pull_indirect, on_error)
            except DeviceUnavailable:
                reason = "device_unavailable"
            except Exception:
                log.exception("audio device setup failed")
                reason = "device_error"
        if reason is not None:
            if device is not None:
                await _stop_device(device)
            await _send_json(ws, {"type": "error", "reason": reason})
            self._loop = None
            return

        try:
            async def receive() -> None:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    last_rx[0] = loop.time()
                    data = message.get("bytes")
                    if data is not None:
                        if len(data) == FRAME_BYTES:
                            jitter.push(data)
                        continue
                    try:
                        control = json.loads(message.get("text") or "")
                    except ValueError:
                        continue
                    if isinstance(control, dict) and control.get("type") == "stop":
                        return

            # Spans ring and live phases unchanged: a caller who cancels mid-ring is handled by
            # the exact same "stop" / disconnect path as one who hangs up mid-call.
            receive_task = asyncio.create_task(receive())

            pickup_mode = await asyncio.to_thread(self._pickup_mode_provider)
            if pickup_mode == "confirm":
                await _send_json(ws, ringing_message())
                confirm_event = asyncio.Event()
                self._pending_decision = None
                self._pending_event = confirm_event
                timeout_task = asyncio.create_task(asyncio.sleep(self._ring_timeout))
                confirm_task = asyncio.create_task(confirm_event.wait())
                done, pending = await asyncio.wait(
                    {confirm_task, timeout_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
                )
                self._pending_event = None

                if receive_task in done:
                    # caller cancelled or disconnected while still ringing
                    for task in (confirm_task, timeout_task):
                        if task in pending:
                            task.cancel()
                    await asyncio.gather(confirm_task, timeout_task, return_exceptions=True)
                    return

                # confirm_task or timeout_task fired; receive_task is untouched here and stays
                # alive for the live phase below (on confirm), or is cancelled just below (on
                # reject/timeout) — see Step 4's note on why it must never be cancelled blindly.
                loser = timeout_task if confirm_task in done else confirm_task
                loser.cancel()
                await asyncio.gather(loser, return_exceptions=True)

                decision = self._pending_decision if confirm_task in done else "timeout"
                if decision != "confirm":
                    await _send_json(ws, rejected_message(decision))
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                    return

            # Live phase: either auto mode, or a confirmed "confirm" mode call.
            pull_box[0] = jitter.pop
            mic_sink_box[0] = on_mic
            last_rx[0] = loop.time()  # idle-timeout counts from here, not from ring start
            await _send_json(ws, ready_message())

            async def send_mic() -> None:
                while True:
                    await ws.send_bytes(await mic_queue.get())

            async def watch() -> str:
                interval = min(0.5, self._idle / 4)
                while True:
                    try:
                        return await asyncio.wait_for(events.get(), timeout=interval)
                    except asyncio.TimeoutError:
                        if loop.time() - last_rx[0] > self._idle:
                            return "idle_timeout"

            watcher = asyncio.create_task(watch())
            tasks = [receive_task, asyncio.create_task(send_mic()), watcher]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if watcher in done and not watcher.cancelled() and watcher.exception() is None:
                await _send_json(ws, {"type": "error", "reason": watcher.result()})
        finally:
            self._loop = None
            await _stop_device(device)


async def _stop_device(device: AudioDevice) -> None:
    """Stop off-loop and bounded, so a hung PortAudio call can never keep the session lock held."""
    try:
        await asyncio.wait_for(asyncio.to_thread(device.stop), timeout=STOP_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.error("audio device stop timed out after %.1fs", STOP_TIMEOUT_S)
    except Exception:
        log.exception("audio device stop failed")


async def _send_json(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_json(payload)
    except (RuntimeError, OSError):
        pass


async def _close(ws: WebSocket) -> None:
    try:
        await ws.close()
    except (RuntimeError, OSError):
        pass
```

- [ ] **Step 6: Run session/protocol tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_session.py tests/test_protocol.py -v`
Expected: PASS, all tests green (existing tests unchanged in behavior since they all use the default `pickup_mode="auto"`).

Run: `cd backend && .venv/bin/pytest tests/test_web.py -q`
Expected: FAIL at this point — `web.py`'s `create_app` still calls `SessionManager(device_factory, config.audio.jitter_ms, config.session.idle_timeout_s)` with only 3 arguments. This is expected; fixed in the next step, in this same task.

- [ ] **Step 7: Write the failing web tests for the new HTTP routes**

In `backend/tests/test_web.py`, extend the import line (it currently reads `from live_intercom.settings import save_telegram_settings` after Task 1 — add to it):

```python
from live_intercom.settings import save_call_confirm_token, save_call_settings, save_telegram_settings
```

Append these tests at the end of the file:

```python
def test_call_confirm_requires_matching_token(client, tmp_path):
    login(client)
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    assert client.get("/api/call/confirm").status_code == 401
    assert client.get("/api/call/confirm?token=wrong").status_code == 401
    response = client.get("/api/call/confirm?token=secret-token")
    assert response.status_code == 200


def test_call_confirm_with_no_token_configured_always_401(client):
    assert client.get("/api/call/confirm?token=").status_code == 401
    assert client.get("/api/call/confirm?token=anything").status_code == 401


def test_call_confirm_with_nothing_pending(client, tmp_path):
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    response = client.get("/api/call/confirm?token=secret-token")
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_pending_call"}


def test_call_reject_requires_matching_token(client, tmp_path):
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    assert client.get("/api/call/reject?token=wrong").status_code == 401
    response = client.get("/api/call/reject?token=secret-token")
    assert response.status_code == 200


def test_call_confirm_via_http_makes_ringing_session_go_live(client, tmp_path):
    save_call_settings(tmp_path / "settings.toml", "confirm")
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    login(client)
    with client.websocket_connect("/ws", headers=cookie_header(client)) as ws:
        assert ws.receive_json() == {"type": "ringing"}
        response = client.get("/api/call/confirm?token=secret-token")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert ws.receive_json() == ready_message()


def test_call_reject_via_http_ends_ringing_session(client, tmp_path):
    save_call_settings(tmp_path / "settings.toml", "confirm")
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    login(client)
    with client.websocket_connect("/ws", headers=cookie_header(client)) as ws:
        assert ws.receive_json() == {"type": "ringing"}
        response = client.get("/api/call/reject?token=secret-token")
        assert response.json() == {"ok": True}
        assert ws.receive_json() == {"type": "rejected", "reason": "declined"}
```

Run: `cd backend && .venv/bin/pytest tests/test_web.py -v -k call_confirm or call_reject`
Expected: FAIL — `404 Not Found` for the new routes, and/or a `TypeError` from `create_app`'s `SessionManager(...)` call still missing the two new required arguments.

- [ ] **Step 8: Wire `SessionManager` and add the confirm/reject routes in `web.py`**

Add `import hmac` to the top of `backend/live_intercom/web.py`, alongside the existing `import asyncio` / `import logging`.

In `create_app`, before the `manager = SessionManager(...)` line, add:

```python
    def get_pickup_mode() -> str:
        try:
            return load_settings(config.settings_file).pickup_mode
        except (OSError, ValueError):
            log.exception(
                "cannot read settings file %s for pickup_mode; defaulting to auto",
                config.settings_file,
            )
            return "auto"
```

Change the `manager = SessionManager(...)` line from:

```python
    manager = SessionManager(device_factory, config.audio.jitter_ms, config.session.idle_timeout_s)
```

to:

```python
    manager = SessionManager(
        device_factory,
        config.audio.jitter_ms,
        config.session.idle_timeout_s,
        pickup_mode_provider=get_pickup_mode,
        ring_timeout_s=config.session.ring_timeout_s,
    )
```

Add these two handlers after `test_telegram` and before `ws_endpoint`:

```python
    async def handle_call_decision(request: Request, act: Callable[[], bool]) -> Response:
        settings = await load_admin_settings()
        if isinstance(settings, Response):
            return settings
        token = request.query_params.get("token", "")
        if not settings.call_confirm_token or not hmac.compare_digest(token, settings.call_confirm_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if act():
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "reason": "no_pending_call"})

    async def call_confirm(request: Request) -> Response:
        return await handle_call_decision(request, manager.confirm)

    async def call_reject(request: Request) -> Response:
        return await handle_call_decision(request, manager.reject)
```

Add the routes to the `routes` list, after `/api/admin/telegram/test` and before `WebSocketRoute("/ws", ws_endpoint)`:

```python
        Route("/api/call/confirm", call_confirm, methods=["GET"]),
        Route("/api/call/reject", call_reject, methods=["GET"]),
```

- [ ] **Step 9: Run the full suite to verify it passes**

Run: `cd backend && .venv/bin/pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 10: Commit**

```bash
git add backend/live_intercom/protocol.py backend/live_intercom/session.py backend/live_intercom/config.py backend/live_intercom/web.py backend/tests/test_protocol.py backend/tests/test_session.py backend/tests/test_config.py backend/tests/test_web.py
git commit -m "feat: add ring/confirm state machine, ring_timeout_s config, and call routes

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Admin call-settings routes (pickup mode, token display/regenerate)

**Files:**
- Modify: `backend/live_intercom/web.py`
- Modify: `backend/tests/test_web.py`

**Interfaces:**
- Consumes: `save_call_settings`, `save_call_confirm_token`, `generate_call_confirm_token` (Task 1); the existing `load_admin_settings()` helper and `get_admin_settings` route (sub-project 1, extended here).
- Produces: `GET /api/admin/settings` response gains `pickup_mode` and `call_confirm_token` (shown in full, auto-generated on first load if unset). `POST /api/admin/call-settings` (body `{pickup_mode}`) → `{ok: true}` or `400`. `POST /api/admin/call-token/regenerate` → `{call_confirm_token: <new token>}`. Task 5 (frontend) consumes all of these exact shapes.

This task changes `GET /api/admin/settings`'s response shape, which breaks 3 existing tests that assert exact dict equality — fixing those is part of this task, not a regression to leave for later.

- [ ] **Step 1: Fix the 3 existing tests broken by the response shape change, and write the new failing tests**

In `backend/tests/test_web.py`, change `test_admin_settings_defaults_when_unset` from:

```python
def test_admin_settings_defaults_when_unset(client):
    login(client)
    body = client.get("/api/admin/settings").json()
    assert body == {"chat_id": "", "token_masked": "", "token_set": False}
```

to:

```python
def test_admin_settings_defaults_when_unset(client):
    login(client)
    body = client.get("/api/admin/settings").json()
    assert body["chat_id"] == ""
    assert body["token_masked"] == ""
    assert body["token_set"] is False
    assert body["pickup_mode"] == "auto"
    assert len(body["call_confirm_token"]) > 20  # auto-generated on first GET
```

Change `test_admin_settings_save_and_roundtrip` from:

```python
def test_admin_settings_save_and_roundtrip(client):
    login(client)
    response = client.post(
        "/api/admin/settings", json={"chat_id": "-1001234567890", "token": "123456:ABCDEFGH"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    body = client.get("/api/admin/settings").json()
    assert body == {"chat_id": "-1001234567890", "token_masked": "••••EFGH", "token_set": True}
```

to:

```python
def test_admin_settings_save_and_roundtrip(client):
    login(client)
    response = client.post(
        "/api/admin/settings", json={"chat_id": "-1001234567890", "token": "123456:ABCDEFGH"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    body = client.get("/api/admin/settings").json()
    assert body["chat_id"] == "-1001234567890"
    assert body["token_masked"] == "••••EFGH"
    assert body["token_set"] is True
```

Change `test_admin_settings_keeps_token_when_masked_value_resubmitted` from:

```python
def test_admin_settings_keeps_token_when_masked_value_resubmitted(client):
    login(client)
    client.post("/api/admin/settings", json={"chat_id": "-1", "token": "123456:ABCDEFGH"})
    masked = client.get("/api/admin/settings").json()["token_masked"]
    response = client.post("/api/admin/settings", json={"chat_id": "-2", "token": masked})
    assert response.status_code == 200
    body = client.get("/api/admin/settings").json()
    assert body == {"chat_id": "-2", "token_masked": "••••EFGH", "token_set": True}
```

to:

```python
def test_admin_settings_keeps_token_when_masked_value_resubmitted(client):
    login(client)
    client.post("/api/admin/settings", json={"chat_id": "-1", "token": "123456:ABCDEFGH"})
    masked = client.get("/api/admin/settings").json()["token_masked"]
    response = client.post("/api/admin/settings", json={"chat_id": "-2", "token": masked})
    assert response.status_code == 200
    body = client.get("/api/admin/settings").json()
    assert body["chat_id"] == "-2"
    assert body["token_masked"] == "••••EFGH"
    assert body["token_set"] is True
```

Append these new tests:

```python
def test_call_settings_requires_login(client):
    assert client.post("/api/admin/call-settings", json={"pickup_mode": "confirm"}).status_code == 401
    assert client.post("/api/admin/call-token/regenerate").status_code == 401


def test_call_settings_save_and_roundtrip(client):
    login(client)
    response = client.post("/api/admin/call-settings", json={"pickup_mode": "confirm"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert client.get("/api/admin/settings").json()["pickup_mode"] == "confirm"


def test_call_settings_rejects_invalid_mode(client):
    login(client)
    response = client.post("/api/admin/call-settings", json={"pickup_mode": "always"})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_pickup_mode"}


def test_call_settings_rejects_bad_body(client):
    login(client)
    assert client.post("/api/admin/call-settings", content=b"not json").status_code == 400
    assert client.post("/api/admin/call-settings", json={}).status_code == 400


def test_call_token_regenerate_returns_new_token_each_time(client):
    login(client)
    first = client.get("/api/admin/settings").json()["call_confirm_token"]
    response = client.post("/api/admin/call-token/regenerate")
    assert response.status_code == 200
    second = response.json()["call_confirm_token"]
    assert second != first
    assert len(second) > 20
    assert client.get("/api/admin/settings").json()["call_confirm_token"] == second


def test_admin_settings_get_auto_generates_token_only_once(client):
    login(client)
    first = client.get("/api/admin/settings").json()["call_confirm_token"]
    second = client.get("/api/admin/settings").json()["call_confirm_token"]
    assert first == second  # not regenerated on every GET, only when unset
```

Run: `cd backend && .venv/bin/pytest tests/test_web.py -v`
Expected: FAIL — `KeyError: 'pickup_mode'` on the 3 fixed tests, `404` on the new routes.

- [ ] **Step 2: Extend `web.py`**

Extend the `.settings` import line (currently, after Task 3, reads `from .settings import load_settings, mask_token, save_telegram_settings`) to:

```python
from .settings import (
    generate_call_confirm_token,
    load_settings,
    mask_token,
    save_call_confirm_token,
    save_call_settings,
    save_telegram_settings,
)
```

Add `from dataclasses import replace` to the top-level imports (alongside `import asyncio` / `import hmac` / `import logging`).

Replace `get_admin_settings` with:

```python
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
                return JSONResponse({"error": "settings_unavailable"}, status_code=503)
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
```

Add these two handlers after `save_admin_settings` and before `test_telegram` (naming the first one `save_call_settings_route` to avoid colliding with the imported `save_call_settings` function from `settings.py`):

```python
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
```

Add the routes to the `routes` list, after `/api/admin/telegram/test` and before `/api/call/confirm`:

```python
        Route("/api/admin/call-settings", save_call_settings_route, methods=["POST"]),
        Route("/api/admin/call-token/regenerate", regenerate_call_token, methods=["POST"]),
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 4: Commit**

```bash
git add backend/live_intercom/web.py backend/tests/test_web.py
git commit -m "feat: add admin routes for pickup mode and call-confirm token

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Frontend — ringing/rejected states, admin "Phone-like calls" section

**Files:**
- Modify: `frontend/src/intercom.ts`
- Modify: `frontend/src/main.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/style.css`

**Interfaces:**
- Consumes: `{"type": "ringing"}`/`{"type": "rejected", "reason": ...}` WS messages (Task 3); `GET /api/admin/settings`'s extended shape, `POST /api/admin/call-settings`, `POST /api/admin/call-token/regenerate` (Task 4).
- Produces: nothing later depends on (terminal task of this plan).

No DOM test harness exists in this project (same constraint as sub-project 1's Task 5) — verified via `npm run build` (strict type check) after each change, plus manual testing. If `npm run build`/`npm test` fail in this sandbox with `nvm`-shell-function recursion errors rather than real toolchain errors, locate the real node binary (`ls -d ~/.nvm/versions/node/*/bin | tail -1`) and invoke `node_modules/typescript/bin/tsc`, `node_modules/vite/bin/vite.js`, and `node_modules/vitest/vitest.mjs` directly with it — a known environment quirk, not a code problem.

- [ ] **Step 1: `intercom.ts` — add the `ringing` state and the `rejected` message**

In `frontend/src/intercom.ts`, change:

```typescript
export type IntercomState = "idle" | "connecting" | "live" | "busy" | "error";
```

to:

```typescript
export type IntercomState = "idle" | "connecting" | "ringing" | "live" | "busy" | "error";
```

Change:

```typescript
const ERROR_TEXT: Record<string, string> = {
  device_unavailable: "The audio device is unavailable.",
  device_lost: "The audio device was disconnected.",
  device_error: "The audio device reported an error.",
  idle_timeout: "Session ended: no audio received.",
};
```

to:

```typescript
const ERROR_TEXT: Record<string, string> = {
  device_unavailable: "The audio device is unavailable.",
  device_lost: "The audio device was disconnected.",
  device_error: "The audio device reported an error.",
  idle_timeout: "Session ended: no audio received.",
  declined: "Call was declined.",
  timeout: "No answer.",
};
```

Change `onControl` from:

```typescript
  private async onControl(message: { type: string; reason?: string }): Promise<void> {
    if (message.type === "ready") {
      await this.startAudio();
    } else if (message.type === "busy") {
      this.finish("busy", "The intercom is in use by another client.");
    } else if (message.type === "error") {
      this.finish("error", ERROR_TEXT[message.reason ?? ""] ?? "Session error.");
    }
  }
```

to:

```typescript
  private async onControl(message: { type: string; reason?: string }): Promise<void> {
    if (message.type === "ready") {
      await this.startAudio();
    } else if (message.type === "ringing") {
      this.onState("ringing");
    } else if (message.type === "busy") {
      this.finish("busy", "The intercom is in use by another client.");
    } else if (message.type === "rejected") {
      this.finish("error", ERROR_TEXT[message.reason ?? ""] ?? "Call ended.");
    } else if (message.type === "error") {
      this.finish("error", ERROR_TEXT[message.reason ?? ""] ?? "Session error.");
    }
  }
```

- [ ] **Step 2: Build check**

Run: `cd frontend && npm run build` (or the direct-node workaround above)
Expected: type errors in `main.ts` — `LABELS` is a `Record<IntercomState, string>` missing the new `"ringing"` key. Expected at this point; fixed in Step 3.

- [ ] **Step 3: `main.ts` — the `ringing` label and treating it as "running"**

Change:

```typescript
const LABELS: Record<IntercomState, string> = {
  idle: "Start talking",
  connecting: "Connecting…",
  live: "Live: tap to stop",
  busy: "In use",
  error: "Start talking",
};
```

to:

```typescript
const LABELS: Record<IntercomState, string> = {
  idle: "Start talking",
  connecting: "Connecting…",
  ringing: "Ringing…",
  live: "Live: tap to stop",
  busy: "In use",
  error: "Start talking",
};
```

In `showIntercom`, change:

```typescript
    running = state === "live" || state === "connecting";
```

to:

```typescript
    running = state === "live" || state === "connecting" || state === "ringing";
```

(This is the only change needed for cancel-while-ringing: the button stays enabled during `"ringing"` already — `button.disabled = state === "connecting";` doesn't list `"ringing"` — so tapping it now correctly calls `intercom.stop()` instead of `intercom.start()`.)

- [ ] **Step 4: Build check**

Run: `cd frontend && npm run build`
Expected: PASS.

- [ ] **Step 5: `api.ts` — extend `AdminSettings`, add call-settings functions**

Change:

```typescript
export interface AdminSettings {
  chat_id: string;
  token_masked: string;
  token_set: boolean;
}
```

to:

```typescript
export interface AdminSettings {
  chat_id: string;
  token_masked: string;
  token_set: boolean;
  pickup_mode: "auto" | "confirm";
  call_confirm_token: string;
}
```

Append to `api.ts`:

```typescript
export type PickupMode = "auto" | "confirm";

export type SaveCallSettingsResult = "ok" | "invalid_pickup_mode" | "error";

export async function saveCallSettings(pickupMode: PickupMode): Promise<SaveCallSettingsResult> {
  const response = await fetch("api/admin/call-settings", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ pickup_mode: pickupMode }),
  });
  if (response.ok) return "ok";
  const body = await response.json().catch(() => ({}));
  return body.error === "invalid_pickup_mode" ? body.error : "error";
}

export async function regenerateCallToken(): Promise<string | null> {
  const response = await fetch("api/admin/call-token/regenerate", { method: "POST" });
  if (!response.ok) return null;
  const body = await response.json();
  return body.call_confirm_token as string;
}
```

- [ ] **Step 6: `main.ts` — the "Phone-like calls" admin section**

Change the import line from:

```typescript
import { getMe, login, logout, getAdminSettings, saveAdminSettings, sendTelegramTest } from "./api";
```

to:

```typescript
import {
  getMe,
  login,
  logout,
  getAdminSettings,
  saveAdminSettings,
  sendTelegramTest,
  saveCallSettings,
  regenerateCallToken,
  type PickupMode,
} from "./api";
```

Replace the whole `showAdmin` function with:

```typescript
function showAdmin(username: string): void {
  const chatId = el("input", { type: "text", placeholder: "Chat ID (e.g. -1001234567890)" });
  const token = el("input", { type: "text", placeholder: "Bot token" });
  const status = el("p", { className: "status" });
  const testButton = el("button", { type: "button", textContent: "Send test message" });
  const back = el("button", { type: "button", className: "link", textContent: "Back" });
  const telegramForm = el(
    "form",
    {},
    el("h1", { textContent: "Settings" }),
    el("h2", { textContent: "Telegram" }),
    chatId,
    token,
    el("button", { textContent: "Save" }),
    testButton,
    status,
    back,
  );

  const pickupMode = el(
    "select",
    {},
    el("option", { value: "auto", textContent: "Automatic pickup" }),
    el("option", { value: "confirm", textContent: "Wait for confirmation" }),
  );
  const callStatus = el("p", { className: "status" });
  const tokenDisplay = el("input", { type: "text", readOnly: true });
  const regenButton = el("button", { type: "button", textContent: "Regenerate" });
  const confirmUrl = el("p", { className: "status" });
  const rejectUrl = el("p", { className: "status" });
  const callForm = el(
    "form",
    {},
    el("h2", { textContent: "Phone-like calls" }),
    pickupMode,
    el("button", { textContent: "Save" }),
    tokenDisplay,
    regenButton,
    confirmUrl,
    rejectUrl,
    callStatus,
  );

  const showToken = (callConfirmToken: string): void => {
    tokenDisplay.value = callConfirmToken;
    confirmUrl.textContent = `Confirm: ${location.origin}/api/call/confirm?token=${callConfirmToken}`;
    rejectUrl.textContent = `Reject: ${location.origin}/api/call/reject?token=${callConfirmToken}`;
  };

  void getAdminSettings()
    .then((settings) => {
      chatId.value = settings.chat_id;
      token.value = settings.token_set ? settings.token_masked : "";
      pickupMode.value = settings.pickup_mode;
      showToken(settings.call_confirm_token);
    })
    .catch(() => {
      status.textContent = "Could not load settings.";
    });

  telegramForm.onsubmit = async (event) => {
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

  callForm.onsubmit = async (event) => {
    event.preventDefault();
    callStatus.textContent = "Saving…";
    const result = await saveCallSettings(pickupMode.value as PickupMode).catch(() => "error" as const);
    callStatus.textContent =
      result === "ok" ? "Saved." :
      result === "invalid_pickup_mode" ? "Invalid mode." :
      "Could not save.";
  };
  regenButton.onclick = async () => {
    callStatus.textContent = "Regenerating…";
    const newToken = await regenerateCallToken().catch(() => null);
    if (newToken) {
      showToken(newToken);
      callStatus.textContent = "Regenerated.";
    } else {
      callStatus.textContent = "Could not regenerate.";
    }
  };

  app.replaceChildren(telegramForm, callForm);
}
```

- [ ] **Step 7: `style.css` — style the new `<select>`**

In `frontend/src/style.css`, add a rule alongside the existing `input { ... }` rule:

```css
select { padding: 0.7rem; font-size: 1rem; }
```

- [ ] **Step 8: Run the build and test suite**

Run: `cd frontend && npm run build`
Expected: PASS, no type errors.

Run: `cd frontend && npm test`
Expected: PASS, existing 8 tests unaffected.

- [ ] **Step 9: Manual verification**

Run the backend (`cd backend && .venv/bin/python -m live_intercom --config config.dev.toml serve`) and frontend dev server (`cd frontend && npm run dev`) as in sub-project 1. In the browser: log in, open Settings, confirm the new "Phone-like calls" section loads with "Automatic pickup" selected and a token already populated (auto-generated), the two confirm/reject URLs shown. Switch to "Wait for confirmation", Save. From a second browser/incognito window, log in and tap the intercom button — confirm it shows "Ringing…" and the Jabra speaker audibly rings. In a terminal, `curl` the confirm URL shown on the admin page and confirm the second window goes live; repeat and `curl` the reject URL instead, confirming it shows "Call was declined." and the button is tappable again.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/intercom.ts frontend/src/main.ts frontend/src/api.ts frontend/src/style.css
git commit -m "feat: add ringing state and admin phone-call settings to the frontend

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Post-plan

`docs/superpowers/specs/2026-09-22-ring-confirm-design.md`'s "Out of scope" section becomes the seed for sub-project 3 (host-initiated proactive call), which will reuse this sub-project's `Settings`/`web.py` patterns and sub-project 1's `telegram.send_message`. It gets its own brainstorming → spec → plan cycle before implementation starts.
