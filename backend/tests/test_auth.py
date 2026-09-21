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
