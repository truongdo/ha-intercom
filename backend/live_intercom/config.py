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
    settings_file: Path
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
