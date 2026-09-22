import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live_intercom.auth import hash_password, save_user
from live_intercom.config import AudioConfig, AuthConfig, Config, SessionConfig
from live_intercom.protocol import ready_message
from live_intercom.settings import save_call_confirm_token, save_call_settings, save_telegram_settings
from live_intercom.web import COOKIE, create_app
from tests.fakes import FakeDevice, wait_for


def make_config(tmp_path, secure_cookie=False, public_url="", ringtone_file=None):
    users = tmp_path / "users.toml"
    save_user(users, "alice", hash_password("pw"))
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
        public_url=public_url,
        ringtone_file=ringtone_file,
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


def test_non_ascii_cookie_is_unauthenticated(client):
    headers = {b"cookie": f"{COOKIE}=abc.d\xe9f".encode("latin-1")}
    assert client.get("/api/me", headers=headers).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=headers):
            pass


def test_login_returns_503_when_users_file_unreadable(client, monkeypatch):
    def boom(path):
        raise PermissionError("denied")

    monkeypatch.setattr("live_intercom.web.load_users", boom)
    response = login(client)
    assert response.status_code == 503
    assert response.json() == {"error": "users_unavailable"}


def test_ws_allows_matching_origin(client):
    login(client)
    headers = {**cookie_header(client), "origin": "http://testserver"}
    with client.websocket_connect("/ws", headers=headers) as ws:
        assert ws.receive_json() == ready_message()


def test_ws_allows_missing_origin(client):
    login(client)
    with client.websocket_connect("/ws", headers=cookie_header(client)) as ws:
        assert ws.receive_json() == ready_message()


def test_ws_refuses_foreign_origin(client):
    login(client)
    headers = {**cookie_header(client), "origin": "https://evil.example"}
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/ws", headers=headers):
            pass
    assert info.value.code == 1008


def test_rate_limit_key_ignores_cf_connecting_ip(client):
    # uvicorn owns proxy headers; a raw cf-connecting-ip must not let a client dodge the limiter.
    for i in range(5):
        response = client.post(
            "/login",
            json={"username": "alice", "password": "bad"},
            headers={"cf-connecting-ip": f"9.9.9.{i}"},
        )
        assert response.status_code == 401
    blocked = client.post(
        "/login",
        json={"username": "alice", "password": "bad"},
        headers={"cf-connecting-ip": "9.9.9.99"},
    )
    assert blocked.status_code == 429


def test_admin_settings_requires_login(client):
    assert client.get("/api/admin/settings").status_code == 401
    assert client.post("/api/admin/settings", json={"chat_id": "-1", "token": "1:a"}).status_code == 401
    assert client.post("/api/admin/telegram/test").status_code == 401


def test_admin_settings_defaults_when_unset(client):
    login(client)
    body = client.get("/api/admin/settings").json()
    assert body["chat_id"] == ""
    assert body["token_masked"] == ""
    assert body["token_set"] is False
    assert body["pickup_mode"] == "auto"
    assert len(body["call_confirm_token"]) > 20  # auto-generated on first GET


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
    save_telegram_settings(tmp_path / "settings.toml", "123456:abc", "-1")
    monkeypatch.setattr("live_intercom.web.send_message", lambda token, chat_id, text: (True, ""))
    response = client.post("/api/admin/telegram/test")
    assert response.json() == {"ok": True, "reason": ""}


def test_telegram_test_failure(client, tmp_path, monkeypatch):
    login(client)
    save_telegram_settings(tmp_path / "settings.toml", "123456:abc", "-1")
    monkeypatch.setattr("live_intercom.web.send_message", lambda token, chat_id, text: (False, "chat not found"))
    response = client.post("/api/admin/telegram/test")
    assert response.json() == {"ok": False, "reason": "chat not found"}


def test_admin_settings_get_returns_503_when_settings_file_corrupted(client, tmp_path):
    login(client)
    (tmp_path / "settings.toml").write_text("not valid toml [[[")
    response = client.get("/api/admin/settings")
    assert response.status_code == 503
    assert response.json() == {"error": "settings_unavailable"}


def test_admin_settings_get_degrades_gracefully_when_token_write_fails(client, monkeypatch):
    login(client)

    def boom(path, token):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("live_intercom.web.save_call_confirm_token", boom)
    response = client.get("/api/admin/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["call_confirm_token"] == ""
    assert body["chat_id"] == ""
    assert body["token_set"] is False


def test_admin_settings_save_returns_503_when_load_fails(client, monkeypatch):
    login(client)

    def boom(path):
        raise OSError("denied")

    monkeypatch.setattr("live_intercom.web.load_settings", boom)
    response = client.post("/api/admin/settings", json={"chat_id": "-1", "token": "123456:ABCDEFGH"})
    assert response.status_code == 503
    assert response.json() == {"error": "settings_unavailable"}


def test_admin_settings_save_returns_503_when_write_fails(client, monkeypatch):
    login(client)

    def boom(path, token, chat_id):
        raise OSError("disk full")

    monkeypatch.setattr("live_intercom.web.save_telegram_settings", boom)
    response = client.post("/api/admin/settings", json={"chat_id": "-1", "token": "123456:ABCDEFGH"})
    assert response.status_code == 503
    assert response.json() == {"error": "settings_unavailable"}


def test_telegram_test_returns_503_when_settings_file_corrupted(client, tmp_path):
    login(client)
    (tmp_path / "settings.toml").write_text("not valid toml [[[")
    response = client.post("/api/admin/telegram/test")
    assert response.status_code == 503
    assert response.json() == {"error": "settings_unavailable"}


def test_call_press_requires_matching_token(client, tmp_path):
    login(client)
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    assert client.get("/api/call/press").status_code == 401
    assert client.get("/api/call/press?token=wrong").status_code == 401
    response = client.get("/api/call/press?token=secret-token")
    assert response.status_code == 200


def test_call_press_with_no_token_configured_always_401(client):
    assert client.get("/api/call/press?token=").status_code == 401
    assert client.get("/api/call/press?token=anything").status_code == 401


def test_call_press_rejects_non_ascii_token_without_crashing(client, tmp_path):
    login(client)
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    response = client.get("/api/call/press?token=caf%C3%A9")  # URL-encoded "café"
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_call_reject_requires_matching_token(client, tmp_path):
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    assert client.get("/api/call/reject?token=wrong").status_code == 401
    response = client.get("/api/call/reject?token=secret-token")
    assert response.status_code == 200


def test_call_press_confirms_a_ringing_call(client, tmp_path):
    save_call_settings(tmp_path / "settings.toml", "confirm")
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    login(client)
    with client.websocket_connect("/ws", headers=cookie_header(client)) as ws:
        assert ws.receive_json() == {"type": "ringing"}
        response = client.get("/api/call/press?token=secret-token")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "action": "confirmed"}
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


def test_call_press_triggers_when_nothing_ringing_and_telegram_unset(client, tmp_path):
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    response = client.get("/api/call/press?token=secret-token")
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "not_configured", "action": "triggered"}


def test_call_press_triggers_and_surfaces_telegram_failure(client, tmp_path, monkeypatch):
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    save_telegram_settings(tmp_path / "settings.toml", "123456:abc", "-1")
    monkeypatch.setattr("live_intercom.web.send_message", lambda token, chat_id, text: (False, "chat not found"))
    response = client.get("/api/call/press?token=secret-token")
    assert response.json() == {"ok": False, "reason": "chat not found", "action": "triggered"}


def test_call_press_triggers_and_sends_message_without_public_url(client, tmp_path, monkeypatch):
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    save_telegram_settings(tmp_path / "settings.toml", "123456:abc", "-1")
    captured = {}

    def fake_send(token, chat_id, text):
        captured["text"] = text
        return True, ""

    monkeypatch.setattr("live_intercom.web.send_message", fake_send)
    response = client.get("/api/call/press?token=secret-token")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "reason": "", "action": "triggered"}
    assert "http" not in captured["text"]
    assert "Someone wants to talk" in captured["text"]


def test_call_press_triggers_and_sends_message_with_public_url(tmp_path, monkeypatch):
    app = create_app(
        make_config(tmp_path, public_url="https://intercom.example.com"), lambda: FakeDevice(), lambda r: True
    )
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    save_telegram_settings(tmp_path / "settings.toml", "123456:abc", "-1")
    captured = {}

    def fake_send(token, chat_id, text):
        captured["text"] = text
        return True, ""

    monkeypatch.setattr("live_intercom.web.send_message", fake_send)
    with TestClient(app) as c:
        response = c.get("/api/call/press?token=secret-token")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "reason": "", "action": "triggered"}
    assert "https://intercom.example.com?go=1" in captured["text"]


def test_call_press_arms_bypass_even_when_telegram_not_configured(client, tmp_path):
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    save_call_settings(tmp_path / "settings.toml", "confirm")
    response = client.get("/api/call/press?token=secret-token")
    assert response.json() == {"ok": False, "reason": "not_configured", "action": "triggered"}
    login(client)
    with client.websocket_connect("/ws", headers=cookie_header(client)) as ws:
        assert ws.receive_json() == ready_message()


def test_call_press_trigger_plays_a_waiting_tone(tmp_path):
    device = FakeDevice()
    app = create_app(make_config(tmp_path), lambda: device, lambda r: True)
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    with TestClient(app) as c:
        response = c.get("/api/call/press?token=secret-token")
        assert response.json()["action"] == "triggered"
        assert wait_for(lambda: device.started)
        frame = device.pull_speaker()
        assert frame != bytes(len(frame))  # tone playing, not silence


def test_call_press_trigger_uses_default_tone_not_custom_ringtone_file(tmp_path):
    import wave

    from live_intercom.audio.ringtone import RingtoneSource
    from live_intercom.protocol import RATE

    ringtone_path = tmp_path / "custom.wav"
    custom_cycle = bytes(range(256)) * 20  # distinct from the default synthesized tone
    with wave.open(str(ringtone_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(custom_cycle)

    device = FakeDevice()
    app = create_app(make_config(tmp_path, ringtone_file=ringtone_path), lambda: device, lambda r: True)
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    with TestClient(app) as c:
        c.get("/api/call/press?token=secret-token")
        assert wait_for(lambda: device.started)
        frame = device.pull_speaker()
        assert frame == RingtoneSource().next_frame()  # the built-in tone, not custom_cycle
        assert frame != custom_cycle[:640]


def test_call_press_waiting_tone_stops_after_bypass_window_expires(tmp_path, monkeypatch):
    monkeypatch.setattr("live_intercom.session.BYPASS_WINDOW_S", 0.2)
    device = FakeDevice()
    app = create_app(make_config(tmp_path), lambda: device, lambda r: True)
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    with TestClient(app) as c:
        c.get("/api/call/press?token=secret-token")
        assert wait_for(lambda: device.started)
        assert wait_for(lambda: device.stopped, timeout=2.0)


def test_call_press_ws_connect_takes_over_the_waiting_tone(tmp_path):
    device = FakeDevice()
    app = create_app(make_config(tmp_path), lambda: device, lambda r: True)
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    with TestClient(app) as c:
        response = c.get("/api/call/press?token=secret-token")
        assert response.json()["action"] == "triggered"
        assert wait_for(lambda: device.started)
        login(c)
        with c.websocket_connect("/ws", headers=cookie_header(c)) as ws:
            assert ws.receive_json() == ready_message()  # bypass still armed -> straight to live


def test_call_press_trigger_skips_tone_when_device_already_busy(tmp_path, monkeypatch):
    device = FakeDevice()
    app = create_app(make_config(tmp_path), lambda: device, lambda r: True)
    save_call_confirm_token(tmp_path / "settings.toml", "secret-token")
    save_telegram_settings(tmp_path / "settings.toml", "123456:abc", "-1")
    monkeypatch.setattr("live_intercom.web.send_message", lambda token, chat_id, text: (True, ""))
    with TestClient(app) as c:
        login(c)
        with c.websocket_connect("/ws", headers=cookie_header(c)) as ws:
            assert ws.receive_json() == ready_message()  # live call already occupies the device
            response = c.get("/api/call/press?token=secret-token")
            assert response.json() == {"ok": True, "reason": "", "action": "triggered"}
            device.emit_mic(bytes(640))
            assert ws.receive_bytes() == bytes(640)  # the live call's own device, untouched
