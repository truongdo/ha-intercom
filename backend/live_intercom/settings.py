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
