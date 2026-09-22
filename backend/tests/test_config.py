from pathlib import Path

import pytest

from live_intercom.config import load_config


def test_defaults(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("")
    cfg = load_config(path)
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.audio.device_match == "Jabra"
    assert cfg.audio.echo_cancel == "off"
    assert cfg.audio.jitter_ms == 60
    assert cfg.auth.session_hours == 12
    assert cfg.auth.secure_cookie is False
    assert cfg.session.idle_timeout_s == 10.0
    assert cfg.auth.users_file == tmp_path / "users.toml"
    assert cfg.settings_file == tmp_path / "settings.toml"


def test_overrides_and_relative_paths(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        'port = 9000\n'
        'static_dir = "web"\n'
        '[audio]\ndevice_match = "Yeti"\necho_cancel = "speex"\n'
        '[auth]\nusers_file = "/etc/u.toml"\nsecure_cookie = true\n'
    )
    cfg = load_config(path)
    assert cfg.port == 9000
    assert cfg.static_dir == tmp_path / "web"
    assert cfg.audio.device_match == "Yeti"
    assert cfg.audio.echo_cancel == "speex"
    assert cfg.auth.users_file == Path("/etc/u.toml")
    assert cfg.auth.secure_cookie is True


def test_settings_file_override(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('settings_file = "/etc/settings.toml"\n')
    cfg = load_config(path)
    assert cfg.settings_file == Path("/etc/settings.toml")


def test_invalid_echo_cancel_rejected(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[audio]\necho_cancel = "magic"\n')
    with pytest.raises(ValueError):
        load_config(path)
