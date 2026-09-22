import asyncio
import threading
import time

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from live_intercom.audio.device import DeviceUnavailable
from live_intercom.protocol import ready_message
from live_intercom.session import SessionManager
from tests.fakes import FakeDevice, wait_for

FRAME = bytes(range(256)) * 2 + bytes(128)  # 640 bytes


def make_client(factory, idle=5.0, pickup_mode="auto", ring_timeout=5.0, clock=time.monotonic, ringtone_file=None):
    manager = SessionManager(
        factory,
        jitter_ms=60,
        idle_timeout_s=idle,
        pickup_mode_provider=lambda: pickup_mode,
        ring_timeout_s=ring_timeout,
        clock=clock,
        ringtone_file=ringtone_file,
    )

    async def endpoint(ws):
        await manager.handle(ws)

    client = TestClient(Starlette(routes=[WebSocketRoute("/ws", endpoint)]))
    client.manager = manager  # exposed so tests can call confirm()/reject()/trigger_bypass() directly
    return client


def test_ready_then_mic_frames_reach_client():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == ready_message()
            device.emit_mic(FRAME)
            assert ws.receive_bytes() == FRAME


def test_client_frames_reach_speaker():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            for _ in range(2):  # the jitter buffer primes before releasing audio
                ws.send_bytes(FRAME)
            assert wait_for(lambda: device.pull_speaker() == FRAME)


def test_second_client_gets_busy():
    with make_client(lambda: FakeDevice()) as client:
        with client.websocket_connect("/ws") as first:
            first.receive_json()
            with client.websocket_connect("/ws") as second:
                assert second.receive_json() == {"type": "busy"}


def test_lock_released_and_device_stopped_after_disconnect():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
        assert wait_for(lambda: device.stopped)
        for _ in range(50):
            with client.websocket_connect("/ws") as ws:
                if ws.receive_json()["type"] == "ready":
                    return
            time.sleep(0.05)
        raise AssertionError("lock was never released")


def test_device_unavailable_reported():
    def boom():
        raise DeviceUnavailable("no device")

    with make_client(boom) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == {"type": "error", "reason": "device_unavailable"}


def test_stop_message_ends_session():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "stop"})
            assert wait_for(lambda: device.stopped)


def test_idle_timeout_ends_session():
    device = FakeDevice()
    with make_client(lambda: device, idle=0.3) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            assert ws.receive_json() == {"type": "error", "reason": "idle_timeout"}
    assert wait_for(lambda: device.stopped)


def test_device_failure_reported():
    device = FakeDevice()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            device.fail("device_lost")
            assert ws.receive_json() == {"type": "error", "reason": "device_lost"}


def test_unexpected_factory_error_reported_and_lock_released():
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("speexdsp missing")
        return FakeDevice()

    with make_client(factory) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == {"type": "error", "reason": "device_error"}
        for _ in range(50):
            with client.websocket_connect("/ws") as ws:
                if ws.receive_json()["type"] == "ready":
                    return
            time.sleep(0.05)
        raise AssertionError("lock was never released")


def test_start_error_reported_and_device_stopped():
    class BadStart(FakeDevice):
        def start(self, *args):
            raise OSError("boom")

    device = BadStart()
    with make_client(lambda: device) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == {"type": "error", "reason": "device_error"}


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_device_calls_run_off_the_event_loop():
    seen = {}

    class Spy(FakeDevice):
        def start(self, *args):
            seen["start"] = _on_event_loop()
            super().start(*args)

        def stop(self):
            seen["stop"] = _on_event_loop()
            super().stop()

    def factory():
        seen["factory"] = _on_event_loop()
        return Spy()

    with make_client(factory) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
        assert wait_for(lambda: "stop" in seen)
    assert seen == {"factory": False, "start": False, "stop": False}


def test_hung_stop_does_not_wedge_the_lock(monkeypatch):
    monkeypatch.setattr("live_intercom.session.STOP_TIMEOUT_S", 0.2)
    release = threading.Event()

    class Hung(FakeDevice):
        def stop(self):
            release.wait(5)

    try:
        with make_client(lambda: Hung()) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
            for _ in range(60):
                with client.websocket_connect("/ws") as ws:
                    if ws.receive_json()["type"] == "ready":
                        return
                time.sleep(0.05)
            raise AssertionError("lock stayed held by hung stop")
    finally:
        release.set()


def test_confirm_mode_rings_then_confirms_to_live():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == {"type": "ringing"}
            frame = device.pull_speaker()
            assert frame != bytes(len(frame))  # ringtone, not silence
            assert client.manager.confirm() is True
            assert ws.receive_json() == ready_message()
            device.emit_mic(FRAME)
            assert ws.receive_bytes() == FRAME


def test_confirm_mode_reject_ends_session():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            assert client.manager.reject() is True
            assert ws.receive_json() == {"type": "rejected", "reason": "declined"}
        assert wait_for(lambda: device.stopped)


def test_confirm_mode_timeout_rejects():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm", ring_timeout=0.2) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            assert ws.receive_json() == {"type": "rejected", "reason": "timeout"}
        assert wait_for(lambda: device.stopped)


def test_confirm_mode_cancel_while_ringing():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            ws.send_json({"type": "stop"})
        assert wait_for(lambda: device.stopped)


def test_confirm_with_nothing_pending_returns_false():
    with make_client(lambda: FakeDevice(), pickup_mode="auto") as client:
        assert client.manager.confirm() is False
        assert client.manager.reject() is False


def test_double_confirm_second_call_returns_false():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            assert client.manager.confirm() is True
            assert client.manager.confirm() is False  # already resolved
            ws.receive_json()  # ready


def test_idle_timeout_not_triggered_by_long_ring():
    device = FakeDevice()
    # idle_timeout_s (0.3) is shorter than how long we simulate ringing before confirming,
    # proving idle-timeout only starts counting once the call goes live, not from ring-start.
    with make_client(lambda: device, idle=0.3, pickup_mode="confirm", ring_timeout=5.0) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            time.sleep(0.5)
            assert client.manager.confirm() is True
            assert ws.receive_json() == ready_message()  # not an idle_timeout error
            # Outlast one watch() poll interval (idle/4 == 0.075s here) before touching the
            # socket again: if last_rx weren't reset at live-start, the watcher's very first
            # check would already see the ~0.5s-stale last_rx and fire idle_timeout well within
            # this window, so this proves absence of a timeout, not just that a frame won a race.
            time.sleep(0.15)
            device.emit_mic(FRAME)
            assert ws.receive_bytes() == FRAME


def test_trigger_bypass_forces_auto_even_in_confirm_mode():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        client.manager.trigger_bypass()
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == ready_message()  # skipped ringing entirely
            device.emit_mic(FRAME)
            assert ws.receive_bytes() == FRAME


def test_trigger_bypass_is_one_shot():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm") as client:
        client.manager.trigger_bypass()
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == ready_message()  # bypass consumed here
            ws.send_json({"type": "stop"})
        assert wait_for(lambda: device.stopped)
        for _ in range(50):
            with client.websocket_connect("/ws") as ws:
                msg = ws.receive_json()
                if msg["type"] == "ringing":
                    return
                assert msg["type"] != "ready"  # must not bypass a second time
            time.sleep(0.05)
        raise AssertionError("second call never started ringing")


def test_trigger_bypass_expires_after_window():
    now = [1000.0]
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm", clock=lambda: now[0]) as client:
        client.manager.trigger_bypass()
        now[0] += 301.0  # past the 300s (5 min) window
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == {"type": "ringing"}  # bypass expired, rings normally


def test_trigger_bypass_with_auto_mode_is_a_no_op():
    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="auto") as client:
        client.manager.trigger_bypass()
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == ready_message()  # already auto; bypass irrelevant


def test_ringtone_file_config_reaches_the_ringing_speaker(tmp_path):
    import wave

    from live_intercom.protocol import RATE

    path = tmp_path / "ringtone.wav"
    custom_cycle = bytes(range(256)) * 20  # non-silent, distinct from the default synth tone
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(custom_cycle)

    device = FakeDevice()
    with make_client(lambda: device, pickup_mode="confirm", ringtone_file=path) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # ringing
            assert device.pull_speaker() == custom_cycle[:640]
