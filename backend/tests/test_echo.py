import pytest

from live_intercom.audio.echo import NullCanceller, make_canceller


def test_null_canceller_passes_near_through():
    assert NullCanceller().process(b"near", b"far") == b"near"


def test_make_canceller_off_is_null():
    assert isinstance(make_canceller("off"), NullCanceller)


def test_make_canceller_rejects_unknown():
    with pytest.raises(ValueError):
        make_canceller("bogus")
