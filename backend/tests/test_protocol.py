from live_intercom import protocol


def test_frame_geometry():
    assert protocol.RATE == 16000
    assert protocol.FRAME_SAMPLES == 320
    assert protocol.FRAME_BYTES == 640
    assert protocol.SILENCE == bytes(640)


def test_ready_message():
    assert protocol.ready_message() == {
        "type": "ready",
        "rate": 16000,
        "channels": 1,
        "frame_ms": 20,
        "codec": "pcm",
    }
    assert protocol.ready_message(protocol.CODEC_OPUS)["codec"] == "opus"


def test_ringing_message():
    assert protocol.ringing_message() == {"type": "ringing"}


def test_rejected_message():
    assert protocol.rejected_message("declined") == {"type": "rejected", "reason": "declined"}
    assert protocol.rejected_message("timeout") == {"type": "rejected", "reason": "timeout"}
