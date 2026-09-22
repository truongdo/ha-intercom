RATE = 16000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2
SILENCE = bytes(FRAME_BYTES)


def ready_message() -> dict:
    return {"type": "ready", "rate": RATE, "channels": CHANNELS, "frame_ms": FRAME_MS}


def ringing_message() -> dict:
    return {"type": "ringing"}


def rejected_message(reason: str) -> dict:
    return {"type": "rejected", "reason": reason}
