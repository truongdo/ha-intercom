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
