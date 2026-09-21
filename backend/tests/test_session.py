import time

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from live_intercom.audio.device import DeviceUnavailable
from live_intercom.protocol import ready_message
from live_intercom.session import SessionManager
from tests.fakes import FakeDevice, wait_for

FRAME = bytes(range(256)) * 2 + bytes(128)  # 640 bytes


def make_client(factory, idle=5.0):
    manager = SessionManager(factory, jitter_ms=60, idle_timeout_s=idle)

    async def endpoint(ws):
        await manager.handle(ws)

    return TestClient(Starlette(routes=[WebSocketRoute("/ws", endpoint)]))


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
