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
