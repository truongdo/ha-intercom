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
