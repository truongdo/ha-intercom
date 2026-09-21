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
