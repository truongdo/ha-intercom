RATE = 16000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2
SILENCE = bytes(FRAME_BYTES)

# Wire codecs for the binary frames: raw FRAME_BYTES of int16 LE PCM, or one raw Opus packet
# per FRAME_MS frame. Negotiated per call: /ws?codec=opus, answered in ready's "codec".
CODEC_PCM = "pcm"
CODEC_OPUS = "opus"


def ready_message(codec: str = CODEC_PCM) -> dict:
    return {"type": "ready", "rate": RATE, "channels": CHANNELS, "frame_ms": FRAME_MS, "codec": codec}


def ringing_message() -> dict:
    return {"type": "ringing"}


def rejected_message(reason: str) -> dict:
    return {"type": "rejected", "reason": reason}
