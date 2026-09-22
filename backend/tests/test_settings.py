import stat

import pytest

from live_intercom.settings import Settings, load_settings, mask_token, save_settings


def test_load_missing_file_returns_defaults(tmp_path):
    assert load_settings(tmp_path / "settings.toml") == Settings()


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.toml"
    save_settings(path, Settings(telegram_bot_token="123456:ABCdef", telegram_chat_id="-1001234567890"))
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "123456:ABCdef"
    assert loaded.telegram_chat_id == "-1001234567890"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_overwrites_previous_values(tmp_path):
    path = tmp_path / "settings.toml"
    save_settings(path, Settings(telegram_bot_token="111:aaa", telegram_chat_id="-1"))
    save_settings(path, Settings(telegram_bot_token="222:bbb", telegram_chat_id="-2"))
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "222:bbb"
    assert loaded.telegram_chat_id == "-2"


@pytest.mark.parametrize("chat_id", ["", "not-a-number", "12.5"])
def test_save_rejects_bad_chat_id(tmp_path, chat_id):
    with pytest.raises(ValueError, match="invalid_chat_id"):
        save_settings(tmp_path / "settings.toml", Settings(telegram_bot_token="123:abc", telegram_chat_id=chat_id))


@pytest.mark.parametrize("token", ["", "no-colon", "abc:def", "123:"])
def test_save_rejects_bad_token(tmp_path, token):
    with pytest.raises(ValueError, match="invalid_token"):
        save_settings(tmp_path / "settings.toml", Settings(telegram_bot_token=token, telegram_chat_id="-1001"))


def test_mask_token():
    assert mask_token("") == ""
    assert mask_token("123456:ABCDEFGH") == "••••EFGH"
