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
