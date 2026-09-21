# Live Intercom Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a web app where a logged-in browser user gets live full-duplex voice with the USB speakerphone on `root@192.168.0.17`.

**Architecture:** One Python process (Starlette + uvicorn) bound to `127.0.0.1:8000` serves a Vite/TypeScript page, login endpoints, and a `/ws` WebSocket carrying 20 ms raw PCM frames. An `audio` module owns the USB device through one full-duplex ALSA stream (sounddevice), with a jitter buffer and an optional echo canceller. A `session` module enforces one active client.

**Tech Stack:** Python 3.11+ (host has 3.13), Starlette, uvicorn, websockets, argon2-cffi, numpy, sounddevice, pytest, httpx; TypeScript, Vite, vitest.

**Spec:** `docs/superpowers/specs/2026-09-21-live-intercom-design.md`

## Global Constraints

- Wire format: 16 kHz, mono, int16 little-endian PCM, 20 ms frames (320 samples, 640 bytes), no header.
- Control messages are JSON text: `ready`, `busy`, `error` (reasons `device_unavailable`, `device_lost`, `idle_timeout`), `stop` from the client.
- App binds to `127.0.0.1:8000` by default; plain HTTP only (Cloudflare Tunnel or SSH forward supplies TLS).
- One active client at a time; idle timeout 10 s; no automatic reconnect.
- Jitter buffer default 60 ms (3 frames); `echo_cancel` defaults to `"off"`; `device_match` defaults to `"Jabra"`.
- Auth: argon2 hashes in `users.toml`, signed `HttpOnly` `SameSite=Lax` cookie, default 12 h, failed-login rate limit per IP, `Secure` flag configurable.
- Host is armv7l (32-bit), Debian 13, Python 3.13: no dependency that needs a Rust toolchain or has no armv7 wheel unless apt provides it (numpy, sounddevice, argon2 come from apt).
- Build the frontend on the dev machine, never on the host.
- Installing apt packages on the host requires the user's explicit approval at that step.
- End every commit message with the trailer `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (second `-m`).
- Deviations from the spec, made for armv7 packaging: use plain Starlette instead of FastAPI (FastAPI pulls pydantic-core, which needs a Rust build on armv7); a WebSocket refused before accept returns HTTP 403, not 401. Task 1 updates the spec to match.

## File Structure

```
backend/pyproject.toml
backend/config.dev.toml
backend/live_intercom/{__init__,__main__,protocol,config,auth,session,web,cli}.py
backend/live_intercom/audio/{__init__,jitter,device,resample,echo,alsa}.py
backend/tests/{__init__,fakes}.py and test_*.py
frontend/{package.json,tsconfig.json,vite.config.ts,index.html}
frontend/public/{capture-worklet.js,playback-worklet.js}
frontend/src/{main,api,intercom,framing,style}.ts|css and framing.test.ts
deploy/{install.sh,remote-setup.sh,live-intercom.service,config.example.toml}
```

---

### Task 1: Scaffold, protocol constants, config

**Files:**
- Create: `.gitignore`, `backend/pyproject.toml`, `backend/live_intercom/__init__.py`, `backend/live_intercom/protocol.py`, `backend/live_intercom/config.py`, `backend/tests/__init__.py`, `backend/tests/test_protocol.py`, `backend/tests/test_config.py`
- Modify: `docs/superpowers/specs/2026-09-21-live-intercom-design.md`

**Interfaces:**
- Produces: `protocol.RATE, CHANNELS, FRAME_MS, FRAME_SAMPLES, FRAME_BYTES, SILENCE, ready_message() -> dict`; `config.AudioConfig(device_match, echo_cancel, jitter_ms)`, `AuthConfig(users_file, secret_file, session_hours, secure_cookie)`, `SessionConfig(idle_timeout_s)`, `Config(host, port, static_dir, audio, auth, session)`, `load_config(path: Path) -> Config`.

- [ ] **Step 1: Create scaffold files**

`.gitignore`:
```
.venv/
__pycache__/
*.egg-info/
node_modules/
frontend/dist/
backend/.dev/
```

`backend/pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "live-intercom"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "starlette>=0.37",
  "uvicorn>=0.30",
  "websockets>=12",
  "argon2-cffi>=23",
  "numpy>=1.24",
  "sounddevice>=0.4.6",
]

[project.optional-dependencies]
dev = ["pytest>=8", "httpx>=0.27"]
speex = ["speexdsp"]

[tool.setuptools.packages.find]
include = ["live_intercom*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`backend/live_intercom/__init__.py` and `backend/tests/__init__.py`: empty files.

- [ ] **Step 2: Write the failing tests**

`backend/tests/test_protocol.py`:
```python
from live_intercom import protocol


def test_frame_geometry():
    assert protocol.RATE == 16000
    assert protocol.FRAME_SAMPLES == 320
    assert protocol.FRAME_BYTES == 640
    assert protocol.SILENCE == bytes(640)


def test_ready_message():
    assert protocol.ready_message() == {
        "type": "ready",
        "rate": 16000,
        "channels": 1,
        "frame_ms": 20,
    }
```

`backend/tests/test_config.py`:
```python
from pathlib import Path

import pytest

from live_intercom.config import load_config


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


def test_overrides_and_relative_paths(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        'port = 9000\n'
        'static_dir = "web"\n'
        '[audio]\ndevice_match = "Yeti"\necho_cancel = "speex"\n'
        '[auth]\nusers_file = "/etc/u.toml"\nsecure_cookie = true\n'
    )
    cfg = load_config(path)
    assert cfg.port == 9000
    assert cfg.static_dir == tmp_path / "web"
    assert cfg.audio.device_match == "Yeti"
    assert cfg.audio.echo_cancel == "speex"
    assert cfg.auth.users_file == Path("/etc/u.toml")
    assert cfg.auth.secure_cookie is True


def test_invalid_echo_cancel_rejected(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[audio]\necho_cancel = "magic"\n')
    with pytest.raises(ValueError):
        load_config(path)
```

- [ ] **Step 3: Set up the venv and run tests to verify they fail**

Run: `cd backend && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q`
Expected: FAIL with `ImportError` / `cannot import name 'protocol'`.

- [ ] **Step 4: Implement**

`backend/live_intercom/protocol.py`:
```python
RATE = 16000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2
SILENCE = bytes(FRAME_BYTES)


def ready_message() -> dict:
    return {"type": "ready", "rate": RATE, "channels": CHANNELS, "frame_ms": FRAME_MS}
```

`backend/live_intercom/config.py`:
```python
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

ECHO_MODES = ("off", "speex")


@dataclass(frozen=True)
class AudioConfig:
    device_match: str = "Jabra"
    echo_cancel: str = "off"
    jitter_ms: int = 60


@dataclass(frozen=True)
class AuthConfig:
    users_file: Path
    secret_file: Path
    session_hours: float = 12
    secure_cookie: bool = False


@dataclass(frozen=True)
class SessionConfig:
    idle_timeout_s: float = 10.0


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    static_dir: Path
    audio: AudioConfig
    auth: AuthConfig
    session: SessionConfig


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    base = path.parent

    def resolve(value: str | None, default: str) -> Path:
        p = Path(value if value is not None else default)
        return p if p.is_absolute() else base / p

    audio_raw = raw.get("audio", {})
    auth_raw = raw.get("auth", {})
    session_raw = raw.get("session", {})

    audio = AudioConfig(
        device_match=audio_raw.get("device_match", "Jabra"),
        echo_cancel=audio_raw.get("echo_cancel", "off"),
        jitter_ms=int(audio_raw.get("jitter_ms", 60)),
    )
    if audio.echo_cancel not in ECHO_MODES:
        raise ValueError(f"echo_cancel must be one of {ECHO_MODES}, got {audio.echo_cancel!r}")

    return Config(
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 8000)),
        static_dir=resolve(raw.get("static_dir"), "static"),
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

- [ ] **Step 5: Correct the spec for the two deviations**

In the spec, replace `FastAPI` with `Starlette` in the Decisions table and Architecture text (note: FastAPI's pydantic-core has no armv7 wheel), and change "WebSocket refused with HTTP 401" to "WebSocket refused before accept (HTTP 403)".

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest -q`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add .gitignore backend docs
git commit -m "feat: scaffold backend, protocol constants and config loader" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

### Task 2: Auth primitives

**Files:**
- Create: `backend/live_intercom/auth.py`, `backend/tests/test_auth.py`

**Interfaces:**
- Produces: `hash_password(password: str) -> str`; `verify_password(users: dict[str, str], username: str, password: str) -> bool`; `load_users(path: Path) -> dict[str, str]` (missing file gives `{}`); `save_user(path: Path, username: str, password_hash: str) -> None` (raises `ValueError` on a bad username, writes mode 0600); `load_or_create_secret(path: Path) -> bytes`; `SessionSigner(secret: bytes, max_age_s: float, clock=time.time)` with `.issue(username) -> str` and `.read(token) -> str | None`; `RateLimiter(max_failures=5, window_s=60.0, clock=time.monotonic)` with `.allowed(key) -> bool`, `.record_failure(key)`, `.reset(key)`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_auth.py`:
```python
import stat

import pytest

from live_intercom.auth import (
    RateLimiter,
    SessionSigner,
    hash_password,
    load_or_create_secret,
    load_users,
    save_user,
    verify_password,
)


def test_hash_and_verify():
    users = {"alice": hash_password("pw")}
    assert verify_password(users, "alice", "pw") is True
    assert verify_password(users, "alice", "nope") is False
    assert verify_password(users, "ghost", "pw") is False


def test_save_and_load_users_roundtrip(tmp_path):
    path = tmp_path / "users.toml"
    assert load_users(path) == {}
    h1, h2 = hash_password("a"), hash_password("b")
    save_user(path, "alice", h1)
    save_user(path, "bob", h2)
    save_user(path, "alice", h2)
    assert load_users(path) == {"alice": h2, "bob": h2}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_user_rejects_bad_username(tmp_path):
    with pytest.raises(ValueError):
        save_user(tmp_path / "u.toml", 'bad"name', "x")


def test_secret_created_once(tmp_path):
    path = tmp_path / "secret.key"
    first = load_or_create_secret(path)
    assert len(first) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_secret(path) == first


def test_signer_roundtrip_and_tamper():
    signer = SessionSigner(b"k" * 32, max_age_s=100)
    token = signer.issue("alice")
    assert signer.read(token) == "alice"
    assert signer.read(token + "x") is None
    assert signer.read("garbage") is None
    assert SessionSigner(b"z" * 32, 100).read(token) is None


def test_signer_expiry():
    now = [1000.0]
    signer = SessionSigner(b"k" * 32, max_age_s=10, clock=lambda: now[0])
    token = signer.issue("alice")
    now[0] = 1009.0
    assert signer.read(token) == "alice"
    now[0] = 1011.0
    assert signer.read(token) is None


def test_rate_limiter_blocks_then_recovers():
    now = [0.0]
    limiter = RateLimiter(max_failures=3, window_s=60, clock=lambda: now[0])
    for _ in range(3):
        assert limiter.allowed("1.2.3.4")
        limiter.record_failure("1.2.3.4")
    assert not limiter.allowed("1.2.3.4")
    assert limiter.allowed("5.6.7.8")
    now[0] = 61.0
    assert limiter.allowed("1.2.3.4")


def test_rate_limiter_reset():
    limiter = RateLimiter(max_failures=1)
    limiter.record_failure("ip")
    assert not limiter.allowed("ip")
    limiter.reset("ip")
    assert limiter.allowed("ip")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_auth.py -q`
Expected: FAIL with `ImportError: cannot import name 'RateLimiter'`.

- [ ] **Step 3: Implement**

`backend/live_intercom/auth.py`:
```python
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import tomllib
from functools import cache
from pathlib import Path
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Modest cost (19 MiB) so verification stays fast on a small ARM board.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


@cache
def _dummy_hash() -> str:
    return _hasher.hash("dummy-password-for-constant-time")


def verify_password(users: dict[str, str], username: str, password: str) -> bool:
    stored = users.get(username)
    if stored is None:
        try:
            _hasher.verify(_dummy_hash(), password)
        except VerificationError:
            pass
        return False
    try:
        return _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def load_users(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(tomllib.loads(path.read_text()).get("users", {}))


def save_user(path: Path, username: str, password_hash: str) -> None:
    if not _USERNAME.match(username):
        raise ValueError(f"invalid username {username!r}")
    users = load_users(path)
    users[username] = password_hash
    lines = ["[users]"] + [
        f"{json.dumps(name)} = {json.dumps(value)}" for name, value in sorted(users.items())
    ]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_or_create_secret(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes()
    secret = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    return secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class SessionSigner:
    def __init__(self, secret: bytes, max_age_s: float, clock: Callable[[], float] = time.time):
        self._secret = secret
        self._max_age = max_age_s
        self._clock = clock

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest())

    def issue(self, username: str) -> str:
        body = json.dumps({"u": username, "exp": self._clock() + self._max_age})
        payload = _b64(body.encode())
        return f"{payload}.{self._sign(payload)}"

    def read(self, token: str) -> str | None:
        try:
            payload, signature = token.rsplit(".", 1)
        except ValueError:
            return None
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < self._clock():
            return None
        user = data.get("u")
        return user if isinstance(user, str) else None


class RateLimiter:
    def __init__(
        self,
        max_failures: int = 5,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max = max_failures
        self._window = window_s
        self._clock = clock
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = self._clock() - self._window
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        self._failures[key] = recent
        return recent

    def allowed(self, key: str) -> bool:
        return len(self._recent(key)) < self._max

    def record_failure(self, key: str) -> None:
        self._recent(key).append(self._clock())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_auth.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat: add auth primitives (argon2 users, signed sessions, rate limiter)" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

### Task 3: Audio building blocks (jitter buffer, device interface, resampling, echo canceller)

**Files:**
- Create: `backend/live_intercom/audio/__init__.py` (empty), `audio/jitter.py`, `audio/device.py`, `audio/resample.py`, `audio/echo.py`, `backend/tests/fakes.py`, `backend/tests/test_jitter.py`, `backend/tests/test_resample.py`, `backend/tests/test_echo.py`

**Interfaces:**
- Consumes: `protocol.FRAME_BYTES`, `protocol.SILENCE`.
- Produces:
  - `JitterBuffer(max_frames: int)` with `.push(frame: bytes)` (drops the oldest on overflow), `.pop() -> bytes` (returns `SILENCE` when empty), `len()`.
  - `DeviceUnavailable(Exception)`; protocol `AudioDevice` with `start(on_mic_frame: Callable[[bytes], None], pull_speaker_frame: Callable[[], bytes], on_error: Callable[[str], None]) -> None` and `stop() -> None`; alias `DeviceFactory = Callable[[], AudioDevice]`. Callbacks run on the audio thread and every frame is `FRAME_BYTES` long.
  - `resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray` (int16 in and out), `to_mono(block: np.ndarray) -> np.ndarray` (shape `(frames, ch)` to `(frames,)`), `to_channels(mono: np.ndarray, channels: int) -> np.ndarray` (to `(frames, channels)`).
  - `EchoCanceller` protocol with `process(near: bytes, far: bytes) -> bytes`; `NullCanceller`; `SpeexCanceller`; `make_canceller(mode: str)`.
  - Test helpers `tests.fakes.FakeDevice` (`emit_mic(frame)`, `pull_speaker()`, `fail(reason)`, `.started`, `.stopped`) and `tests.fakes.wait_for(predicate, timeout=2.0) -> bool`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_jitter.py`:
```python
from live_intercom.audio.jitter import JitterBuffer
from live_intercom.protocol import SILENCE

A, B, C, D = (bytes([n]) * 640 for n in (1, 2, 3, 4))


def test_fifo_order():
    buf = JitterBuffer(3)
    buf.push(A)
    buf.push(B)
    assert len(buf) == 2
    assert buf.pop() == A
    assert buf.pop() == B


def test_silence_when_empty():
    assert JitterBuffer(3).pop() == SILENCE


def test_overflow_drops_oldest():
    buf = JitterBuffer(3)
    for frame in (A, B, C, D):
        buf.push(frame)
    assert [buf.pop(), buf.pop(), buf.pop()] == [B, C, D]
```

`backend/tests/test_resample.py`:
```python
import numpy as np

from live_intercom.audio.resample import resample, to_channels, to_mono


def test_same_rate_is_identity():
    x = np.arange(320, dtype=np.int16)
    assert np.array_equal(resample(x, 16000, 16000), x)


def test_downsample_48k_to_16k_averages_triplets():
    x = np.array([3, 6, 9, 30, 30, 30], dtype=np.int16)
    out = resample(x, 48000, 16000)
    assert out.dtype == np.int16
    assert out.tolist() == [6, 30]


def test_frame_lengths_for_common_rates():
    for rate in (48000, 44100, 32000, 24000, 8000):
        block = np.zeros(rate * 20 // 1000, dtype=np.int16)
        assert len(resample(block, rate, 16000)) == 320
        assert len(resample(np.zeros(320, dtype=np.int16), 16000, rate)) == len(block)


def test_upsample_interpolates_linearly():
    x = np.array([0, 300], dtype=np.int16)
    out = resample(x, 16000, 48000)
    assert out.tolist() == [0, 100, 200, 300, 300, 300]


def test_to_mono_averages_channels():
    block = np.array([[10, 20], [30, 50]], dtype=np.int16)
    assert to_mono(block).tolist() == [15, 40]


def test_to_channels_duplicates():
    out = to_channels(np.array([1, 2], dtype=np.int16), 2)
    assert out.shape == (2, 2)
    assert out.tolist() == [[1, 1], [2, 2]]
```

`backend/tests/test_echo.py`:
```python
import pytest

from live_intercom.audio.echo import NullCanceller, make_canceller


def test_null_canceller_passes_near_through():
    assert NullCanceller().process(b"near", b"far") == b"near"


def test_make_canceller_off_is_null():
    assert isinstance(make_canceller("off"), NullCanceller)


def test_make_canceller_rejects_unknown():
    with pytest.raises(ValueError):
        make_canceller("bogus")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_jitter.py tests/test_resample.py tests/test_echo.py -q`
Expected: FAIL with `ModuleNotFoundError: live_intercom.audio`.

- [ ] **Step 3: Implement the modules**

`backend/live_intercom/audio/jitter.py`:
```python
from __future__ import annotations

import threading
from collections import deque

from ..protocol import SILENCE


class JitterBuffer:
    """Thread-safe bounded FIFO of audio frames; underrun yields silence."""

    def __init__(self, max_frames: int):
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        self._max = max_frames
        self._frames: deque[bytes] = deque()
        self._lock = threading.Lock()

    def push(self, frame: bytes) -> None:
        with self._lock:
            if len(self._frames) >= self._max:
                self._frames.popleft()
            self._frames.append(frame)

    def pop(self) -> bytes:
        with self._lock:
            return self._frames.popleft() if self._frames else SILENCE

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
```

`backend/live_intercom/audio/device.py`:
```python
from __future__ import annotations

from typing import Callable, Protocol


class DeviceUnavailable(Exception):
    """The audio device is missing or cannot be opened."""


class AudioDevice(Protocol):
    def start(
        self,
        on_mic_frame: Callable[[bytes], None],
        pull_speaker_frame: Callable[[], bytes],
        on_error: Callable[[str], None],
    ) -> None: ...

    def stop(self) -> None: ...


DeviceFactory = Callable[[], AudioDevice]
```

`backend/live_intercom/audio/resample.py`:
```python
from __future__ import annotations

import numpy as np


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return samples
    if src_rate % dst_rate == 0:
        factor = src_rate // dst_rate
        usable = len(samples) // factor * factor
        grouped = samples[:usable].astype(np.int32).reshape(-1, factor)
        return grouped.mean(axis=1).astype(np.int16)
    out_len = int(len(samples) * dst_rate / src_rate)
    positions = np.arange(out_len) * (src_rate / dst_rate)
    return np.rint(np.interp(positions, np.arange(len(samples)), samples)).astype(np.int16)


def to_mono(block: np.ndarray) -> np.ndarray:
    if block.shape[1] == 1:
        return block[:, 0]
    return block.astype(np.int32).mean(axis=1).astype(np.int16)


def to_channels(mono: np.ndarray, channels: int) -> np.ndarray:
    return np.repeat(mono.reshape(-1, 1), channels, axis=1)
```

`backend/live_intercom/audio/echo.py`:
```python
from __future__ import annotations

from typing import Protocol

from ..protocol import FRAME_SAMPLES, RATE

# 2048 samples at 16 kHz is 128 ms of echo tail; tune on the real device.
FILTER_LENGTH = 2048


class EchoCanceller(Protocol):
    def process(self, near: bytes, far: bytes) -> bytes: ...


class NullCanceller:
    def process(self, near: bytes, far: bytes) -> bytes:
        return near


class SpeexCanceller:
    def __init__(self) -> None:
        try:
            from speexdsp import EchoCanceller as _Speex
        except ImportError as exc:
            raise RuntimeError(
                "echo_cancel = 'speex' needs the optional 'speexdsp' package "
                "(pip install speexdsp; requires libspeexdsp-dev to build)"
            ) from exc
        self._ec = _Speex.create(FRAME_SAMPLES, FILTER_LENGTH, RATE)

    def process(self, near: bytes, far: bytes) -> bytes:
        return self._ec.process(near, far)


def make_canceller(mode: str) -> EchoCanceller:
    if mode == "off":
        return NullCanceller()
    if mode == "speex":
        return SpeexCanceller()
    raise ValueError(f"unknown echo_cancel mode {mode!r}")
```

`backend/tests/fakes.py`:
```python
from __future__ import annotations

import time
from typing import Callable


class FakeDevice:
    """In-memory AudioDevice. Tests drive the mic and inspect the speaker side."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self._on_mic: Callable[[bytes], None] | None = None
        self._pull: Callable[[], bytes] | None = None
        self._on_error: Callable[[str], None] | None = None

    def start(self, on_mic_frame, pull_speaker_frame, on_error) -> None:
        self._on_mic, self._pull, self._on_error = on_mic_frame, pull_speaker_frame, on_error
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def emit_mic(self, frame: bytes) -> None:
        assert self._on_mic is not None
        self._on_mic(frame)

    def pull_speaker(self) -> bytes:
        assert self._pull is not None
        return self._pull()

    def fail(self, reason: str) -> None:
        assert self._on_error is not None
        self._on_error(reason)


def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat: add jitter buffer, device interface, resampling and echo canceller seam" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

### Task 4: Session manager (WebSocket bridge with one-client lock)

**Files:**
- Create: `backend/live_intercom/session.py`, `backend/tests/test_session.py`

**Interfaces:**
- Consumes: `JitterBuffer`, `DeviceFactory`, `DeviceUnavailable`, `protocol.FRAME_BYTES, FRAME_MS, ready_message`, `tests.fakes.FakeDevice, wait_for`.
- Produces: `SessionManager(device_factory: DeviceFactory, jitter_ms: int, idle_timeout_s: float)` with `async handle(ws: starlette.websockets.WebSocket) -> None` (accepts the socket, runs one session, always releases the lock and stops the device) and property `active: bool`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_session.py`:
```python
import time

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from live_intercom.audio.device import DeviceUnavailable
from live_intercom.protocol import ready_message
from live_intercom.session import SessionManager
from tests.fakes import FakeDevice, wait_for

FRAME = bytes(range(256)) * 2 + bytes(128)  # 640 bytes


def make_client(factory, idle=5.0):
    manager = SessionManager(factory, jitter_ms=60, idle_timeout_s=idle)

    async def endpoint(ws):
        await manager.handle(ws)

    return TestClient(Starlette(routes=[WebSocketRoute("/ws", endpoint)]))


def test_ready_then_mic_frames_reach_client():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == ready_message()
            device.emit_mic(FRAME)
            assert ws.receive_bytes() == FRAME


def test_client_frames_reach_speaker():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_bytes(FRAME)
            assert wait_for(lambda: device.pull_speaker() == FRAME)


def test_second_client_gets_busy():
    with make_client(lambda: FakeDevice()) as client:
        with client.websocket_connect("/ws") as first:
            first.receive_json()
            with client.websocket_connect("/ws") as second:
                assert second.receive_json() == {"type": "busy"}


def test_lock_released_and_device_stopped_after_disconnect():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
        assert wait_for(lambda: device.stopped)
        for _ in range(50):
            with client.websocket_connect("/ws") as ws:
                if ws.receive_json()["type"] == "ready":
                    return
            time.sleep(0.05)
        raise AssertionError("lock was never released")


def test_device_unavailable_reported():
    def boom():
        raise DeviceUnavailable("no device")

    with make_client(boom) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == {"type": "error", "reason": "device_unavailable"}


def test_stop_message_ends_session():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "stop"})
            assert wait_for(lambda: device.stopped)


def test_idle_timeout_ends_session():
    device = FakeDevice()
    with make_client(lambda: device, idle=0.3) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            assert ws.receive_json() == {"type": "error", "reason": "idle_timeout"}
    assert wait_for(lambda: device.stopped)


def test_device_failure_reported():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            device.fail("device_lost")
            assert ws.receive_json() == {"type": "error", "reason": "device_lost"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_session.py -q`
Expected: FAIL with `ModuleNotFoundError: live_intercom.session`.

- [ ] **Step 3: Implement**

`backend/live_intercom/session.py`:
```python
from __future__ import annotations

import asyncio
import json

from starlette.websockets import WebSocket

from .audio.device import AudioDevice, DeviceFactory, DeviceUnavailable
from .audio.jitter import JitterBuffer
from .protocol import FRAME_BYTES, FRAME_MS, ready_message

MIC_QUEUE_FRAMES = 50


class SessionManager:
    def __init__(self, device_factory: DeviceFactory, jitter_ms: int, idle_timeout_s: float):
        self._device_factory = device_factory
        self._max_frames = max(1, jitter_ms // FRAME_MS)
        self._idle = idle_timeout_s
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

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
        jitter = JitterBuffer(self._max_frames)
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_FRAMES)
        events: asyncio.Queue[str] = asyncio.Queue()
        last_rx = [loop.time()]

        def offer(frame: bytes) -> None:
            if mic_queue.full():
                mic_queue.get_nowait()  # drop oldest: real-time audio prefers a glitch to delay
            mic_queue.put_nowait(frame)

        def on_mic(frame: bytes) -> None:
            loop.call_soon_threadsafe(offer, frame)

        def on_error(reason: str) -> None:
            loop.call_soon_threadsafe(events.put_nowait, reason)

        device: AudioDevice | None = None
        try:
            device = self._device_factory()
            device.start(on_mic, jitter.pop, on_error)
        except DeviceUnavailable:
            await _send_json(ws, {"type": "error", "reason": "device_unavailable"})
            device = None
            return

        try:
            await _send_json(ws, ready_message())

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
            tasks = [asyncio.create_task(receive()), asyncio.create_task(send_mic()), watcher]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if watcher in done and not watcher.cancelled() and watcher.exception() is None:
                await _send_json(ws, {"type": "error", "reason": watcher.result()})
        finally:
            if device is not None:
                device.stop()


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

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_session.py -q`
Expected: 8 passed. Run it three times (`for i in 1 2 3; do .venv/bin/pytest tests/test_session.py -q; done`) to check for flakiness; all runs must pass.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat: add WebSocket session manager with single-client lock" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

### Task 5: Web app (login, session cookie, status, WebSocket wiring)

**Files:**
- Create: `backend/live_intercom/web.py`, `backend/tests/test_web.py`

**Interfaces:**
- Consumes: `Config`, `auth.*`, `SessionManager`, `DeviceFactory`.
- Produces: `create_app(config: Config, device_factory: DeviceFactory, audio_probe: Callable[[bool], bool]) -> Starlette`. The probe argument is `refresh` (True when no session is active, so the probe may rescan for hot-plugged devices). Routes: `POST /login` (JSON `{username, password}`; 200 sets cookie `intercom_session`; 400 bad body; 401 bad credentials; 429 too many attempts), `POST /logout`, `GET /api/me` (401 or `{"username", "audio_available"}`), `WS /ws` (closed before accept without a valid cookie), static files at `/` when `config.static_dir` exists.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_web.py`:
```python
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live_intercom.auth import hash_password, save_user
from live_intercom.config import AudioConfig, AuthConfig, Config, SessionConfig
from live_intercom.protocol import ready_message
from live_intercom.web import COOKIE, create_app
from tests.fakes import FakeDevice


def make_config(tmp_path, secure_cookie=False):
    users = tmp_path / "users.toml"
    save_user(users, "alice", hash_password("pw"))
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


@pytest.fixture
def client(tmp_path):
    app = create_app(make_config(tmp_path), lambda: FakeDevice(), lambda refresh: True)
    with TestClient(app) as c:
        yield c


def login(client, password="pw"):
    return client.post("/login", json={"username": "alice", "password": password})


def cookie_header(client):
    return {"cookie": f"{COOKIE}={client.cookies[COOKIE]}"}


def test_login_success_sets_httponly_cookie(client):
    response = login(client)
    assert response.status_code == 200
    header = response.headers["set-cookie"]
    assert COOKIE in header and "HttpOnly" in header and "SameSite=lax" in header
    assert "Secure" not in header


def test_secure_cookie_flag_configurable(tmp_path):
    app = create_app(make_config(tmp_path, secure_cookie=True), lambda: FakeDevice(), lambda r: True)
    with TestClient(app) as c:
        assert "Secure" in login(c).headers["set-cookie"]


def test_login_bad_password_and_bad_body(client):
    assert login(client, "wrong").status_code == 401
    assert client.post("/login", json={"username": "alice"}).status_code == 400
    assert client.post("/login", content=b"not json").status_code == 400


def test_login_rate_limited_after_repeated_failures(client):
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    assert login(client, "wrong").status_code == 429
    assert login(client, "pw").status_code == 429


def test_me_requires_login_and_reports_audio(client):
    assert client.get("/api/me").status_code == 401
    login(client)
    body = client.get("/api/me").json()
    assert body == {"username": "alice", "audio_available": True}


def test_logout_clears_session(client):
    login(client)
    client.post("/logout")
    assert client.get("/api/me").status_code == 401


def test_tampered_cookie_rejected(client):
    login(client)
    client.cookies.set(COOKIE, client.cookies[COOKIE] + "x")
    assert client.get("/api/me").status_code == 401


def test_ws_refused_without_login(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_ws_ready_after_login(client):
    login(client)
    with client.websocket_connect("/ws", headers=cookie_header(client)) as ws:
        assert ws.receive_json() == ready_message()


def test_static_files_served_when_directory_exists(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<h1>hi</h1>")
    app = create_app(make_config(tmp_path), lambda: FakeDevice(), lambda r: True)
    with TestClient(app) as c:
        assert "<h1>hi</h1>" in c.get("/").text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_web.py -q`
Expected: FAIL with `ModuleNotFoundError: live_intercom.web`.

- [ ] **Step 3: Implement**

`backend/live_intercom/web.py`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest -q`
Expected: all tests pass. If `SameSite=lax` fails to match because Starlette capitalizes it as `SameSite=lax`, check the actual header printed by the failure and adjust only the test's expected substring.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat: add web app with login, session cookie, status and ws endpoint" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

### Task 6: ALSA device and CLI (`serve`, `add-user`, `list-devices`, `loopback`)

**Files:**
- Create: `backend/live_intercom/audio/alsa.py`, `backend/live_intercom/cli.py`, `backend/live_intercom/__main__.py`, `backend/config.dev.toml`, `backend/tests/test_alsa_selection.py`, `backend/tests/test_cli.py`

**Interfaces:**
- Consumes: `AudioConfig`, `resample/to_mono/to_channels`, `make_canceller`, `DeviceUnavailable`, `create_app`, `auth.*`, `load_config`.
- Produces: `alsa.pick_device(devices: list[dict], match: str) -> int` (first duplex device whose name contains `match`, case-insensitive, else `DeviceUnavailable`); `alsa.choose_rate(supports: Callable[[int], bool], candidates=CANDIDATE_RATES) -> int`; `alsa.AlsaDevice` (implements `AudioDevice`); `alsa.open_alsa_device(cfg: AudioConfig) -> AlsaDevice`; `alsa.probe_audio(cfg: AudioConfig, refresh: bool) -> bool`; `cli.main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_alsa_selection.py`:
```python
import pytest

from live_intercom.audio.alsa import choose_rate, pick_device
from live_intercom.audio.device import DeviceUnavailable

DEVICES = [
    {"name": "HDMI Out", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Jabra SPEAK 710: USB Audio (hw:2,0)", "max_input_channels": 1, "max_output_channels": 2},
    {"name": "Jabra Mic Only", "max_input_channels": 1, "max_output_channels": 0},
]


def test_pick_device_matches_case_insensitively_and_requires_duplex():
    assert pick_device(DEVICES, "jabra") == 1


def test_pick_device_missing_raises():
    with pytest.raises(DeviceUnavailable):
        pick_device(DEVICES, "Yeti")
    with pytest.raises(DeviceUnavailable):
        pick_device(DEVICES[:1], "HDMI")  # output-only is not duplex


def test_choose_rate_prefers_earliest_supported_candidate():
    assert choose_rate(lambda r: r in (48000, 44100)) == 48000
    assert choose_rate(lambda r: True) == 16000


def test_choose_rate_none_supported_raises():
    with pytest.raises(DeviceUnavailable):
        choose_rate(lambda r: False)
```

`backend/tests/test_cli.py`:
```python
from live_intercom.auth import load_users, verify_password
from live_intercom.cli import main


def write_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[auth]\nusers_file = "users.toml"\nsecret_file = "secret.key"\n')
    return path


def test_add_user_writes_verifiable_hash(tmp_path, monkeypatch):
    answers = iter(["s3cret", "s3cret"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["--config", str(write_config(tmp_path)), "add-user", "bob"]) == 0
    assert verify_password(load_users(tmp_path / "users.toml"), "bob", "s3cret")


def test_add_user_password_mismatch_fails(tmp_path, monkeypatch):
    answers = iter(["one", "two"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["--config", str(write_config(tmp_path)), "add-user", "bob"]) == 1
    assert not (tmp_path / "users.toml").exists()


def test_missing_config_exits_with_error(tmp_path):
    assert main(["--config", str(tmp_path / "nope.toml"), "add-user", "bob"]) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_alsa_selection.py tests/test_cli.py -q`
Expected: FAIL with `ModuleNotFoundError: live_intercom.audio.alsa`.

- [ ] **Step 3: Implement `alsa.py`**

`backend/live_intercom/audio/alsa.py`:
```python
from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from ..config import AudioConfig
from ..protocol import FRAME_MS, RATE
from .device import DeviceUnavailable
from .echo import EchoCanceller, make_canceller
from .resample import resample, to_channels, to_mono

log = logging.getLogger(__name__)

CANDIDATE_RATES = (16000, 48000, 44100, 32000, 24000, 8000)


def pick_device(devices: list[dict], match: str) -> int:
    needle = match.lower()
    for index, info in enumerate(devices):
        if (
            needle in info["name"].lower()
            and info["max_input_channels"] > 0
            and info["max_output_channels"] > 0
        ):
            return index
    raise DeviceUnavailable(f"no duplex audio device matching {match!r}")


def choose_rate(supports: Callable[[int], bool], candidates=CANDIDATE_RATES) -> int:
    for rate in candidates:
        if supports(rate):
            return rate
    raise DeviceUnavailable("device supports none of the candidate sample rates")


def _refresh_portaudio(sd) -> None:
    # PortAudio caches the device list at init; re-init to see hot-plugged devices.
    # Only call when no stream is open.
    sd._terminate()
    sd._initialize()


def _supports(sd, index: int, rate: int, in_ch: int, out_ch: int) -> bool:
    try:
        sd.check_input_settings(device=index, channels=in_ch, dtype="int16", samplerate=rate)
        sd.check_output_settings(device=index, channels=out_ch, dtype="int16", samplerate=rate)
        return True
    except sd.PortAudioError:
        return False


class AlsaDevice:
    def __init__(self, index: int, rate: int, in_ch: int, out_ch: int, canceller: EchoCanceller):
        self._index, self._rate = index, rate
        self._in, self._out = in_ch, out_ch
        self._canceller = canceller
        self._block = rate * FRAME_MS // 1000
        self._stream = None
        self._stopping = False

    @property
    def rate(self) -> int:
        return self._rate

    def start(self, on_mic_frame, pull_speaker_frame, on_error) -> None:
        import sounddevice as sd

        self._on_mic, self._pull, self._on_error = on_mic_frame, pull_speaker_frame, on_error
        self._stopping = False
        try:
            self._stream = sd.RawStream(
                samplerate=self._rate,
                blocksize=self._block,
                device=(self._index, self._index),
                channels=(self._in, self._out),
                dtype="int16",
                callback=self._callback,
                finished_callback=self._finished,
            )
            self._stream.start()
        except sd.PortAudioError as exc:
            self._stream = None
            raise DeviceUnavailable(str(exc)) from exc

    def stop(self) -> None:
        self._stopping = True
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # device may already be gone
                log.exception("error while closing audio stream")

    def _finished(self) -> None:
        if not self._stopping:
            self._on_error("device_lost")

    def _callback(self, indata, outdata, frames, time_info, status) -> None:
        try:
            if status:
                log.debug("audio status: %s", status)
            mic = np.frombuffer(indata, dtype=np.int16).reshape(-1, self._in)
            mic16 = resample(to_mono(mic), self._rate, RATE)
            speaker = self._pull()
            cleaned = self._canceller.process(mic16.tobytes(), speaker)
            self._on_mic(cleaned)
            out = resample(np.frombuffer(speaker, dtype=np.int16), RATE, self._rate)
            outdata[:] = to_channels(out, self._out).tobytes()
        except Exception:
            log.exception("audio callback failed")
            outdata[:] = bytes(len(outdata))
            self._on_error("device_error")


def open_alsa_device(cfg: AudioConfig) -> AlsaDevice:
    import sounddevice as sd

    _refresh_portaudio(sd)  # the session manager guarantees no stream is open here
    devices = [dict(d) for d in sd.query_devices()]
    index = pick_device(devices, cfg.device_match)
    in_ch = min(devices[index]["max_input_channels"], 2)
    out_ch = min(devices[index]["max_output_channels"], 2)
    rate = choose_rate(lambda r: _supports(sd, index, r, in_ch, out_ch))
    log.info("using %s at %d Hz (%d in, %d out)", devices[index]["name"], rate, in_ch, out_ch)
    return AlsaDevice(index, rate, in_ch, out_ch, make_canceller(cfg.echo_cancel))


def probe_audio(cfg: AudioConfig, refresh: bool) -> bool:
    try:
        import sounddevice as sd

        if refresh:
            _refresh_portaudio(sd)
        pick_device([dict(d) for d in sd.query_devices()], cfg.device_match)
        return True
    except Exception:
        return False
```

- [ ] **Step 4: Implement the CLI**

`backend/live_intercom/__main__.py`:
```python
from .cli import main

raise SystemExit(main())
```

`backend/live_intercom/cli.py`:
```python
from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from .auth import hash_password, save_user
from .config import Config, load_config


def cmd_serve(cfg: Config) -> int:
    import uvicorn

    from .audio.alsa import open_alsa_device, probe_audio
    from .web import create_app

    app = create_app(
        cfg,
        lambda: open_alsa_device(cfg.audio),
        lambda refresh: probe_audio(cfg.audio, refresh),
    )
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )
    return 0


def cmd_add_user(cfg: Config, username: str) -> int:
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        print("passwords do not match", file=sys.stderr)
        return 1
    if not password:
        print("password must not be empty", file=sys.stderr)
        return 1
    try:
        save_user(cfg.auth.users_file, username, hash_password(password))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"saved user {username!r} to {cfg.auth.users_file}")
    return 0


def cmd_list_devices() -> int:
    import sounddevice as sd

    from .audio.alsa import CANDIDATE_RATES, _supports

    for index, info in enumerate(sd.query_devices()):
        in_ch, out_ch = info["max_input_channels"], info["max_output_channels"]
        line = f"[{index}] {info['name']}  in={in_ch} out={out_ch}"
        if in_ch and out_ch:
            ch_in, ch_out = min(in_ch, 2), min(out_ch, 2)
            rates = [r for r in CANDIDATE_RATES if _supports(sd, index, r, ch_in, ch_out)]
            line += f"  duplex rates={rates}"
        print(line)
    return 0


def cmd_loopback(cfg: Config) -> int:
    import numpy as np

    from .audio.alsa import open_alsa_device
    from .protocol import FRAME_SAMPLES, RATE

    device = open_alsa_device(cfg.audio)
    t = np.arange(FRAME_SAMPLES * 200)
    tone = (8000 * np.sin(2 * np.pi * 440 * t / RATE)).astype(np.int16)
    position = [0]
    captured: list[bytes] = []

    def pull() -> bytes:
        start = position[0] * FRAME_SAMPLES
        position[0] = (position[0] + 1) % 200
        return tone[start : start + FRAME_SAMPLES].tobytes()

    device.start(captured.append, pull, lambda reason: print("error:", reason))
    print(f"playing a 440 Hz tone for 3 s at {device.rate} Hz, recording the mic...")
    time.sleep(3)
    device.stop()
    samples = np.frombuffer(b"".join(captured), dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        print("no audio captured")
        return 1
    print(f"captured {samples.size} samples, rms={np.sqrt((samples ** 2).mean()):.0f}, peak={np.abs(samples).max():.0f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live-intercom")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the web server")
    add_user = sub.add_parser("add-user", help="create or update a user")
    add_user.add_argument("username")
    sub.add_parser("list-devices", help="print audio devices and supported rates")
    sub.add_parser("loopback", help="play a tone and record the mic to test the device")
    args = parser.parse_args(argv)

    if args.command == "list-devices":
        return cmd_list_devices()
    if not args.config.exists():
        print(f"config file not found: {args.config}", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    if args.command == "serve":
        return cmd_serve(cfg)
    if args.command == "add-user":
        return cmd_add_user(cfg, args.username)
    return cmd_loopback(cfg)
```

`backend/config.dev.toml`:
```toml
static_dir = "../frontend/dist"

[audio]
device_match = "Jabra"
echo_cancel = "off"

[auth]
users_file = ".dev/users.toml"
secret_file = ".dev/secret.key"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && mkdir -p .dev && .venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Smoke-test the CLI locally**

Run: `cd backend && .venv/bin/python -m live_intercom list-devices`
Expected: prints this machine's audio devices without a traceback.

Then create a dev user. `getpass` needs a TTY, so the user runs this in the prompt: `! cd backend && .venv/bin/python -m live_intercom --config config.dev.toml add-user dev` and enters a password twice. Expected: `saved user 'dev'`.

- [ ] **Step 7: Commit**

```bash
git add backend
git commit -m "feat: add ALSA duplex device and CLI (serve, add-user, list-devices, loopback)" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

### Task 7: Frontend (login, mic button, audio pipeline)

**Files:**
- Create: `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/index.html`, `frontend/public/capture-worklet.js`, `frontend/public/playback-worklet.js`, `frontend/src/framing.ts`, `frontend/src/framing.test.ts`, `frontend/src/api.ts`, `frontend/src/intercom.ts`, `frontend/src/main.ts`, `frontend/src/style.css`

**Interfaces:**
- Consumes: backend `POST /login`, `POST /logout`, `GET /api/me`, `WS /ws` and the wire format from Global Constraints.
- Produces: `framing.ts`: `floatToInt16(f: Float32Array): Int16Array`, `int16ToFloat(i: Int16Array): Float32Array`, `class StreamResampler(srcRate: number, dstRate: number)` with `process(input: Float32Array): Float32Array`, `class Framer(frameSamples: number)` with `push(samples: Int16Array): Int16Array[]`. `intercom.ts`: `class Intercom(onState: (state: IntercomState, detail?: string) => void)` with `start(): Promise<void>` and `stop(): void`; `type IntercomState = "idle" | "connecting" | "live" | "busy" | "error"`.

- [ ] **Step 1: Scaffold the project**

`frontend/package.json`:
```json
{
  "name": "live-intercom-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "test": "vitest run"
  },
  "devDependencies": {
    "typescript": "^5.5.0",
    "vite": "^5.4.0",
    "vitest": "^2.0.0"
  }
}
```

`frontend/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "strict": true,
    "noUnusedLocals": true,
    "skipLibCheck": true
  },
  "include": ["src"]
}
```

`frontend/vite.config.ts`:
```ts
import { defineConfig } from "vite";

const backend = "http://127.0.0.1:8000";

export default defineConfig({
  base: "./",
  server: {
    proxy: {
      "/api": backend,
      "/login": backend,
      "/logout": backend,
      "/ws": { target: backend.replace("http", "ws"), ws: true },
    },
  },
});
```

`frontend/index.html`:
```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Live Intercom</title>
  </head>
  <body>
    <main id="app"></main>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
```

Run: `cd frontend && npm install`
Expected: installs without errors.

- [ ] **Step 2: Write the failing tests**

`frontend/src/framing.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { Framer, StreamResampler, floatToInt16, int16ToFloat } from "./framing";

describe("int16/float conversion", () => {
  it("clamps and scales", () => {
    expect(Array.from(floatToInt16(new Float32Array([0, 1, -1, 2, -2])))).toEqual([
      0, 32767, -32768, 32767, -32768,
    ]);
  });

  it("round-trips within one step", () => {
    const back = int16ToFloat(floatToInt16(new Float32Array([0.5, -0.25])));
    expect(back[0]).toBeCloseTo(0.5, 3);
    expect(back[1]).toBeCloseTo(-0.25, 3);
  });
});

describe("StreamResampler", () => {
  it("decimates by two as a continuous stream", () => {
    const r = new StreamResampler(32000, 16000);
    const first = r.process(Float32Array.from([1, 2, 3, 4, 5, 6, 7, 8]));
    const second = r.process(Float32Array.from([9, 10, 11, 12, 13, 14, 15, 16]));
    expect([...first, ...second]).toEqual([0, 2, 4, 6, 8, 10, 12, 14]);
  });

  it("gives the same output regardless of chunking", () => {
    const input = Float32Array.from({ length: 480 }, (_, i) => Math.sin(i / 7));
    const whole = new StreamResampler(48000, 16000).process(input);
    const chunked = new StreamResampler(48000, 16000);
    const parts: number[] = [];
    for (let i = 0; i < input.length; i += 128) {
      parts.push(...chunked.process(input.slice(i, i + 128)));
    }
    expect(parts.length).toBe(whole.length);
    parts.forEach((v, i) => expect(v).toBeCloseTo(whole[i], 6));
  });

  it("upsamples by interpolation", () => {
    const r = new StreamResampler(16000, 32000);
    const out = r.process(Float32Array.from([2, 4]));
    // The stream starts from an implicit previous sample of 0: 0->2 passes 1, 2->4 passes 3.
    expect([...out]).toEqual([0, 1, 2, 3]);
  });
});

describe("Framer", () => {
  it("emits fixed-size frames and keeps the remainder", () => {
    const f = new Framer(4);
    expect(f.push(Int16Array.from([1, 2, 3]))).toEqual([]);
    const frames = f.push(Int16Array.from([4, 5, 6, 7, 8, 9]));
    expect(frames.map((x) => Array.from(x))).toEqual([
      [1, 2, 3, 4],
      [5, 6, 7, 8],
    ]);
    expect(f.push(Int16Array.from([10, 11, 12])).map((x) => Array.from(x))).toEqual([
      [9, 10, 11, 12],
    ]);
  });
});
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run`
Expected: FAIL, `Failed to resolve import "./framing"`.

- [ ] **Step 4: Implement `framing.ts`**

`frontend/src/framing.ts`:
```ts
export function floatToInt16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? Math.round(s * 32768) : Math.round(s * 32767);
  }
  return out;
}

export function int16ToFloat(input: Int16Array): Float32Array {
  const out = new Float32Array(input.length);
  for (let i = 0; i < input.length; i++) {
    out[i] = input[i] / (input[i] < 0 ? 32768 : 32767);
  }
  return out;
}

/** Stateful linear-interpolation resampler for chunked streams. */
export class StreamResampler {
  private readonly step: number;
  private pos = 0;
  private last = 0;

  constructor(srcRate: number, dstRate: number) {
    this.step = srcRate / dstRate;
  }

  process(input: Float32Array): Float32Array {
    const out: number[] = [];
    // Virtual buffer v = [last, ...input]; pos indexes into v.
    while (Math.floor(this.pos) + 1 <= input.length) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      const a = i === 0 ? this.last : input[i - 1];
      const b = input[i];
      out.push(a * (1 - frac) + b * frac);
      this.pos += this.step;
    }
    this.pos -= input.length;
    if (input.length > 0) this.last = input[input.length - 1];
    return Float32Array.from(out);
  }
}

/** Collects samples into fixed-size frames. */
export class Framer {
  private pending: number[] = [];

  constructor(private readonly frameSamples: number) {}

  push(samples: Int16Array): Int16Array[] {
    for (const s of samples) this.pending.push(s);
    const frames: Int16Array[] = [];
    while (this.pending.length >= this.frameSamples) {
      frames.push(Int16Array.from(this.pending.splice(0, this.frameSamples)));
    }
    return frames;
  }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd frontend && npx vitest run`
Expected: all framing tests pass.

- [ ] **Step 6: Implement the worklets**

`frontend/public/capture-worklet.js`:
```js
// Forwards each 128-sample mono block to the main thread, which resamples and frames it.
class CaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice());
    return true;
  }
}
registerProcessor("capture", CaptureProcessor);
```

`frontend/public/playback-worklet.js`:
```js
// Plays queued Float32 chunks; silence on underrun, drops the oldest audio when over ~120 ms.
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.buffered = 0;
    this.maxBuffered = Math.round(sampleRate * 0.12);
    this.port.onmessage = (event) => {
      this.queue.push(event.data);
      this.buffered += event.data.length;
      while (this.buffered > this.maxBuffered && this.queue.length > 1) {
        const dropped = this.queue.shift();
        this.buffered -= dropped.length - this.offset;
        this.offset = 0;
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    for (let i = 0; i < out.length; i++) {
      if (this.queue.length === 0) {
        out[i] = 0;
        continue;
      }
      const head = this.queue[0];
      out[i] = head[this.offset++];
      this.buffered--;
      if (this.offset >= head.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor("playback", PlaybackProcessor);
```

- [ ] **Step 7: Implement `api.ts` and `intercom.ts`**

`frontend/src/api.ts`:
```ts
export interface Me {
  username: string;
  audio_available: boolean;
}

export async function getMe(): Promise<Me | null> {
  const response = await fetch("api/me");
  if (response.status === 401) return null;
  if (!response.ok) throw new Error(`status check failed (${response.status})`);
  return response.json();
}

export type LoginResult = "ok" | "invalid" | "rate_limited" | "error";

export async function login(username: string, password: string): Promise<LoginResult> {
  const response = await fetch("login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (response.ok) return "ok";
  if (response.status === 401) return "invalid";
  if (response.status === 429) return "rate_limited";
  return "error";
}

export async function logout(): Promise<void> {
  await fetch("logout", { method: "POST" });
}
```

`frontend/src/intercom.ts`:
```ts
import { Framer, StreamResampler, floatToInt16, int16ToFloat } from "./framing";

const WIRE_RATE = 16000;
const FRAME_SAMPLES = 320;

export type IntercomState = "idle" | "connecting" | "live" | "busy" | "error";
type OnState = (state: IntercomState, detail?: string) => void;

const ERROR_TEXT: Record<string, string> = {
  device_unavailable: "The audio device is unavailable.",
  device_lost: "The audio device was disconnected.",
  device_error: "The audio device reported an error.",
  idle_timeout: "Session ended: no audio received.",
};

export class Intercom {
  private ws: WebSocket | null = null;
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private playback: AudioWorkletNode | null = null;
  private upsampler: StreamResampler | null = null;
  private finished = false;

  constructor(private readonly onState: OnState) {}

  async start(): Promise<void> {
    this.finished = false;
    this.onState("connecting");
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
    } catch {
      this.finish("error", "Microphone permission was denied.");
      return;
    }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        void this.onControl(JSON.parse(event.data));
      } else if (this.playback && this.upsampler) {
        const samples = this.upsampler.process(int16ToFloat(new Int16Array(event.data)));
        this.playback.port.postMessage(samples, [samples.buffer]);
      }
    };
    ws.onerror = () => this.finish("error", "Could not connect to the server.");
    ws.onclose = () => {
      if (this.ws === ws) this.finish("idle");
    };
  }

  stop(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "stop" }));
    }
    this.finish("idle");
  }

  private async onControl(message: { type: string; reason?: string }): Promise<void> {
    if (message.type === "ready") {
      await this.startAudio();
    } else if (message.type === "busy") {
      this.finish("busy", "The intercom is in use by another client.");
    } else if (message.type === "error") {
      this.finish("error", ERROR_TEXT[message.reason ?? ""] ?? "Session error.");
    }
  }

  private async startAudio(): Promise<void> {
    if (!this.stream || !this.ws || this.finished) return;
    const ws = this.ws;
    const ctx = new AudioContext({ latencyHint: "interactive" });
    this.ctx = ctx;
    await ctx.audioWorklet.addModule("capture-worklet.js");
    await ctx.audioWorklet.addModule("playback-worklet.js");
    if (this.finished) return;

    const source = ctx.createMediaStreamSource(this.stream);
    const capture = new AudioWorkletNode(ctx, "capture");
    const mute = ctx.createGain();
    mute.gain.value = 0; // keeps the capture node pulled by the graph without local monitoring
    source.connect(capture);
    capture.connect(mute).connect(ctx.destination);

    const downsampler = new StreamResampler(ctx.sampleRate, WIRE_RATE);
    const framer = new Framer(FRAME_SAMPLES);
    capture.port.onmessage = (event) => {
      const pcm = floatToInt16(downsampler.process(event.data as Float32Array));
      for (const frame of framer.push(pcm)) {
        if (ws.readyState === WebSocket.OPEN) ws.send(frame);
      }
    };

    this.playback = new AudioWorkletNode(ctx, "playback", { outputChannelCount: [1] });
    this.playback.connect(ctx.destination);
    this.upsampler = new StreamResampler(WIRE_RATE, ctx.sampleRate);
    await ctx.resume();
    this.onState("live");
  }

  private finish(state: IntercomState, detail?: string): void {
    if (this.finished) return;
    this.finished = true;
    const ws = this.ws;
    this.ws = null;
    ws?.close();
    this.stream?.getTracks().forEach((track) => track.stop());
    void this.ctx?.close();
    this.stream = this.ctx = this.playback = this.upsampler = null;
    this.onState(state, detail);
  }
}
```

- [ ] **Step 8: Implement `main.ts` and `style.css`**

`frontend/src/main.ts`:
```ts
import "./style.css";
import { getMe, login, logout } from "./api";
import { Intercom, type IntercomState } from "./intercom";

const app = document.getElementById("app")!;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Partial<HTMLElementTagNameMap[K]> = {},
  ...children: (Node | string)[]
): HTMLElementTagNameMap[K] {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function showLogin(message = ""): void {
  const user = el("input", { type: "text", autocomplete: "username", required: true, placeholder: "Username" });
  const pass = el("input", { type: "password", autocomplete: "current-password", required: true, placeholder: "Password" });
  const status = el("p", { className: "status", textContent: message });
  const form = el("form", {}, el("h1", { textContent: "Live Intercom" }), user, pass, el("button", { textContent: "Sign in" }), status);
  form.onsubmit = async (event) => {
    event.preventDefault();
    const result = await login(user.value, pass.value).catch(() => "error" as const);
    if (result === "ok") return void start();
    status.textContent =
      result === "invalid" ? "Wrong username or password." :
      result === "rate_limited" ? "Too many attempts. Try again in a minute." :
      "Sign-in failed.";
  };
  app.replaceChildren(form);
}

const LABELS: Record<IntercomState, string> = {
  idle: "Start talking",
  connecting: "Connecting…",
  live: "Live: tap to stop",
  busy: "In use",
  error: "Start talking",
};

function showIntercom(username: string, audioAvailable: boolean): void {
  const button = el("button", { className: "mic", textContent: LABELS.idle });
  const status = el("p", { className: "status", textContent: audioAvailable ? "" : "Audio device unavailable." });
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
  signOut.onclick = async () => { intercom.stop(); await logout(); showLogin(); };
  app.replaceChildren(el("h1", { textContent: "Live Intercom" }), button, status, signOut);
}

async function start(): Promise<void> {
  try {
    const me = await getMe();
    if (me) showIntercom(me.username, me.audio_available);
    else showLogin();
  } catch {
    showLogin("Could not reach the server.");
  }
}

void start();
```

`frontend/src/style.css`:
```css
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; }
main { width: min(22rem, 100% - 2rem); display: grid; gap: 1rem; text-align: center; }
form { display: grid; gap: 0.75rem; }
input { padding: 0.7rem; font-size: 1rem; }
button { padding: 0.7rem; font-size: 1rem; cursor: pointer; }
button:disabled { cursor: not-allowed; opacity: 0.6; }
.mic { width: 12rem; height: 12rem; border-radius: 50%; border: 0; margin: 0 auto; font-size: 1.1rem; background: #2563eb; color: #fff; }
.mic[data-state="live"] { background: #dc2626; animation: pulse 1.6s infinite; }
.mic[data-state="busy"] { background: #6b7280; }
.link { background: none; border: 0; text-decoration: underline; }
.status { min-height: 1.4em; margin: 0; }
@keyframes pulse { 50% { box-shadow: 0 0 0 1rem rgba(220, 38, 38, 0.2); } }
```

- [ ] **Step 9: Build and test**

Run: `cd frontend && npm test && npm run build`
Expected: tests pass; build writes `frontend/dist/` with `index.html` and both worklet files.

- [ ] **Step 10: Commit**

```bash
git add frontend
git commit -m "feat: add frontend with login, mic button and audio pipeline" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

### Task 8: Deployment files

**Files:**
- Create: `deploy/config.example.toml`, `deploy/live-intercom.service`, `deploy/remote-setup.sh`, `deploy/install.sh`

**Interfaces:**
- Consumes: the `live_intercom` package, the built `frontend/dist`, the CLI.
- Produces: `deploy/install.sh` run from the dev machine (`HOST=root@192.168.0.17 INSTALL_APT=0|1 deploy/install.sh`) that builds the frontend, copies the app to `/opt/live-com-ha` on the host with tar over ssh (no rsync needed), and runs `deploy/remote-setup.sh` there. Host layout: code in `/opt/live-com-ha` (venv in `venv/`, static files in `frontend-dist/`), config in `/etc/live-intercom/config.toml`, state (users, secret) in `/var/lib/live-intercom/`, systemd unit `live-intercom.service` running as user `intercom` (group `audio`).

- [ ] **Step 1: Write the deployment files**

`deploy/config.example.toml`:
```toml
host = "127.0.0.1"
port = 8000
static_dir = "/opt/live-com-ha/frontend-dist"

[audio]
device_match = "Jabra"
echo_cancel = "off"   # "speex" needs the optional speexdsp package
jitter_ms = 60

[auth]
users_file = "/var/lib/live-intercom/users.toml"
secret_file = "/var/lib/live-intercom/secret.key"
session_hours = 12
secure_cookie = true   # true behind Cloudflare Tunnel; set false to test over plain http://localhost

[session]
idle_timeout_s = 10
```

`deploy/live-intercom.service`:
```ini
[Unit]
Description=Live Intercom
After=network.target sound.target

[Service]
User=intercom
Group=audio
WorkingDirectory=/var/lib/live-intercom
ExecStart=/opt/live-com-ha/venv/bin/python -m live_intercom --config /etc/live-intercom/config.toml serve
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/live-intercom
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`deploy/remote-setup.sh`:
```bash
#!/usr/bin/env bash
# Runs on the host. INSTALL_APT=1 installs system packages (needs the owner's approval).
set -euo pipefail
DEST=/opt/live-com-ha

if [ "${INSTALL_APT:-0}" = "1" ]; then
  apt-get update
  apt-get install -y python3-venv python3-numpy python3-sounddevice python3-argon2 libportaudio2
fi

id intercom >/dev/null 2>&1 || useradd --system --home /var/lib/live-intercom --shell /usr/sbin/nologin -G audio intercom
install -d -o intercom -g intercom -m 0750 /var/lib/live-intercom
install -d -m 0755 /etc/live-intercom
[ -f /etc/live-intercom/config.toml ] || install -m 0644 "$DEST/deploy/config.example.toml" /etc/live-intercom/config.toml

# --system-site-packages picks up the apt-provided numpy, sounddevice and argon2 (no armv7 wheels to build).
[ -d "$DEST/venv" ] || python3 -m venv --system-site-packages "$DEST/venv"
"$DEST/venv/bin/pip" install --upgrade "$DEST/backend"

install -m 0644 "$DEST/deploy/live-intercom.service" /etc/systemd/system/live-intercom.service
systemctl daemon-reload
systemctl enable live-intercom
systemctl restart live-intercom
systemctl --no-pager status live-intercom | head -n 12
```

`deploy/install.sh`:
```bash
#!/usr/bin/env bash
# Runs on the dev machine. Usage: HOST=root@192.168.0.17 [INSTALL_APT=1] deploy/install.sh
set -euo pipefail
HOST="${HOST:-root@192.168.0.17}"
DEST=/opt/live-com-ha
cd "$(dirname "$0")/.."

(cd frontend && npm ci && npm run build)

ssh "$HOST" "mkdir -p $DEST/backend $DEST/frontend-dist $DEST/deploy && rm -rf $DEST/backend/live_intercom $DEST/frontend-dist/*"
tar -C backend -czf - live_intercom pyproject.toml | ssh "$HOST" "tar -xzf - -C $DEST/backend"
tar -C frontend/dist -czf - . | ssh "$HOST" "tar -xzf - -C $DEST/frontend-dist"
tar -C deploy -czf - config.example.toml live-intercom.service remote-setup.sh | ssh "$HOST" "tar -xzf - -C $DEST/deploy"
ssh "$HOST" "INSTALL_APT=${INSTALL_APT:-0} bash $DEST/deploy/remote-setup.sh"
```

- [ ] **Step 2: Check the scripts parse**

Run: `chmod +x deploy/install.sh deploy/remote-setup.sh && bash -n deploy/install.sh && bash -n deploy/remote-setup.sh`
Expected: no output (syntax OK).

- [ ] **Step 3: Commit**

```bash
git add deploy
git commit -m "feat: add systemd unit and one-command deploy scripts" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: Bring-up and verification on `root@192.168.0.17`

This task changes the host. Ask the user before Step 1 (apt packages), and again before Step 4 if `echo_cancel = "speex"` is wanted.

**Files:** none created. Fixes found here go back into the earlier tasks' files with their own commits.

- [ ] **Step 1: Ask approval, then deploy with system packages**

Ask: "OK to run apt-get install of `python3-venv python3-numpy python3-sounddevice python3-argon2 libportaudio2` and create the `intercom` user and systemd service on 192.168.0.17?" On yes, run: `INSTALL_APT=1 deploy/install.sh`
Expected: pip installs `live-intercom`, the final status block shows `active (running)`. If pip tries to compile a package and fails, note which package, and install its `python3-*` apt equivalent.

- [ ] **Step 2: Create the first user on the host**

The user runs (needs a TTY): `! ssh -t root@192.168.0.17 'runuser -u intercom -- /opt/live-com-ha/venv/bin/python -m live_intercom --config /etc/live-intercom/config.toml add-user <name>'`
Expected: `saved user '<name>'`.

- [ ] **Step 3: Confirm the audio device and rates (resolves spec risk 1)**

Run: `ssh root@192.168.0.17 '/opt/live-com-ha/venv/bin/python -m live_intercom list-devices'`
Expected: a line for the Jabra with `duplex rates=[...]`. Record the rates in the spec's risk section. If no rate is listed, or the Jabra shows no duplex entry, stop and debug with `arecord -l`, `aplay -l` and `groups intercom` (must include `audio`).

Then the hardware loopback: `ssh root@192.168.0.17 'runuser -u intercom -- /opt/live-com-ha/venv/bin/python -m live_intercom --config /etc/live-intercom/config.toml loopback'`
Expected: the Jabra plays a tone for 3 s, and the output reports `rms` well above 0 (for example over 100). If the rms is near 0, the mic is muted or the wrong device was picked; check `alsamixer -c 2`.

- [ ] **Step 4: Measure CPU (resolves spec risk 2)**

Run while a session is live (Step 6): `ssh root@192.168.0.17 'top -b -n 3 -d 2 | grep -E "python|Cpu"'`
Expected: the Python process well under 50% of one core. Record the number in the spec's risk section.

- [ ] **Step 5: Reach the app locally through an SSH forward**

The user runs: `! ssh -N -L 8000:127.0.0.1:8000 root@192.168.0.17` (in a second terminal), then opens `http://localhost:8000`. For this plain-http test, set `secure_cookie = false` in `/etc/live-intercom/config.toml` on the host and `systemctl restart live-intercom` (set it back to `true` for the tunnel).
Expected: login page appears; wrong password shows an error; the right one shows the mic button.

- [ ] **Step 6: Run the manual checklist**

Use Chrome. Tick each:
- [ ] Press the mic button, allow the microphone: the button turns red and says "Live".
- [ ] Speak in the browser: the voice comes out of the Jabra.
- [ ] Speak near the Jabra: the voice plays in the browser.
- [ ] Open a second browser or private window, sign in, press the button: it shows "In use", and the first session is unaffected.
- [ ] Press the button again in the first window: the Jabra goes silent, and the second window can now connect.
- [ ] Close the tab mid-session: within a few seconds a new session can start.
- [ ] Unplug the Jabra mid-session: the page shows the device was disconnected. Replug and reload: the mic button is enabled again without restarting the service.
- [ ] Echo check with `echo_cancel = "off"`: talk from the browser for 30 s; there is no howl or repeating echo. If there is, set `echo_cancel = "speex"`, install `libspeexdsp-dev` and `pip install speexdsp` into the venv (ask the user first), restart, and repeat. Verify that `speexdsp.EchoCanceller.create(frame_size, filter_length, sample_rate)` and `.process(near_bytes, far_bytes)` exist with `python -c "import speexdsp; help(speexdsp.EchoCanceller)"` and adjust `SpeexCanceller` in `audio/echo.py` if the API differs.
- [ ] Latency feels acceptable on the SSH forward. If choppy, raise `jitter_ms` to 80 or 100 in the config.

- [ ] **Step 7: Test through Cloudflare**

The user points `cloudflared` at `http://localhost:8000` (their own tunnel config) with `secure_cookie = true`. Open the tunnel's `https://` URL and repeat the first three checklist items.
Expected: works over `wss://`. Note the added latency for the user.

- [ ] **Step 8: Record results and commit**

Update the spec's Risks section with the measured rates, CPU use and echo results, then:
```bash
git add docs
git commit -m "docs: record host verification results" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

