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
            if not isinstance(body, dict):
                return False, "telegram_error"
            return False, str(body.get("description", "telegram_error"))[:200]
        except (ValueError, UnicodeDecodeError):
            return False, "telegram_error"
    except (URLError, TimeoutError, OSError):
        log.warning("telegram send_message network error", exc_info=True)
        return False, "network_error"
    except ValueError:
        return False, "telegram_error"
    if not isinstance(body, dict):
        return False, "telegram_error"
    if not body.get("ok", False):
        return False, str(body.get("description", "telegram_error"))[:200]
    return True, ""
