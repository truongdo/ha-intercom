import stat

import pytest

from live_intercom.settings import (
    Settings,
    generate_call_confirm_token,
    load_settings,
    mask_token,
    save_call_confirm_token,
    save_call_settings,
    save_telegram_settings,
)


def test_load_missing_file_returns_defaults(tmp_path):
    assert load_settings(tmp_path / "settings.toml") == Settings()


def test_save_telegram_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.toml"
    save_telegram_settings(path, "123456:ABCdef", "-1001234567890")
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "123456:ABCdef"
    assert loaded.telegram_chat_id == "-1001234567890"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_telegram_overwrites_previous_values(tmp_path):
    path = tmp_path / "settings.toml"
    save_telegram_settings(path, "111:aaa", "-1")
    save_telegram_settings(path, "222:bbb", "-2")
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "222:bbb"
    assert loaded.telegram_chat_id == "-2"


@pytest.mark.parametrize("chat_id", ["", "not-a-number", "12.5"])
def test_save_telegram_rejects_bad_chat_id(tmp_path, chat_id):
    with pytest.raises(ValueError, match="invalid_chat_id"):
        save_telegram_settings(tmp_path / "settings.toml", "123:abc", chat_id)


@pytest.mark.parametrize("token", ["", "no-colon", "abc:def", "123:"])
def test_save_telegram_rejects_bad_token(tmp_path, token):
    with pytest.raises(ValueError, match="invalid_token"):
        save_telegram_settings(tmp_path / "settings.toml", token, "-1001")


def test_mask_token():
    assert mask_token("") == ""
    assert mask_token("123456:ABCDEFGH") == "••••EFGH"


def test_save_call_settings_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.toml"
    save_call_settings(path, "confirm")
    assert load_settings(path).pickup_mode == "confirm"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", ["", "Auto", "always", "off"])
def test_save_call_settings_rejects_bad_mode(tmp_path, mode):
    with pytest.raises(ValueError, match="invalid_pickup_mode"):
        save_call_settings(tmp_path / "settings.toml", mode)


def test_save_call_confirm_token_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.toml"
    save_call_confirm_token(path, "test-token-value")
    assert load_settings(path).call_confirm_token == "test-token-value"


def test_generate_call_confirm_token_is_random_and_long():
    a, b = generate_call_confirm_token(), generate_call_confirm_token()
    assert a != b
    assert len(a) > 20


def test_telegram_and_call_sections_save_independently(tmp_path):
    path = tmp_path / "settings.toml"
    save_telegram_settings(path, "123456:abc", "-1")
    save_call_settings(path, "confirm")
    save_call_confirm_token(path, "known-token")
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "123456:abc"
    assert loaded.telegram_chat_id == "-1"
    assert loaded.pickup_mode == "confirm"
    assert loaded.call_confirm_token == "known-token"

    save_telegram_settings(path, "999999:zzz", "-2")
    loaded = load_settings(path)
    assert loaded.telegram_bot_token == "999999:zzz"
    assert loaded.telegram_chat_id == "-2"
    assert loaded.pickup_mode == "confirm"  # untouched by the telegram-only save
    assert loaded.call_confirm_token == "known-token"  # untouched
