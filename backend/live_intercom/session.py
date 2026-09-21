from __future__ import annotations

import asyncio
import json

from starlette.websockets import WebSocket

from .audio.device import AudioDevice, DeviceFactory, DeviceUnavailable
from .audio.jitter import JitterBuffer
from .protocol import FRAME_BYTES, FRAME_MS, ready_message

MIC_QUEUE_FRAMES = 50


class SessionManager:
    def __init__(self, device_factory: DeviceFactory, jitter_ms: int, idle_timeout_s: float):
        self._device_factory = device_factory
        self._max_frames = max(1, jitter_ms // FRAME_MS)
        self._idle = idle_timeout_s
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        # Single-threaded event loop: no await between check and set, so this is atomic.
        if self._active:
            await _send_json(ws, {"type": "busy"})
            await _close(ws)
            return
        self._active = True
        try:
            await self._run(ws)
        finally:
            self._active = False
            await _close(ws)

    async def _run(self, ws: WebSocket) -> None:
        loop = asyncio.get_running_loop()
        jitter = JitterBuffer(self._max_frames)
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_FRAMES)
        events: asyncio.Queue[str] = asyncio.Queue()
        last_rx = [loop.time()]

        def offer(frame: bytes) -> None:
            if mic_queue.full():
                mic_queue.get_nowait()  # drop oldest: real-time audio prefers a glitch to delay
            mic_queue.put_nowait(frame)

        def on_mic(frame: bytes) -> None:
            loop.call_soon_threadsafe(offer, frame)

        def on_error(reason: str) -> None:
            loop.call_soon_threadsafe(events.put_nowait, reason)

        device: AudioDevice | None = None
        try:
            device = self._device_factory()
            device.start(on_mic, jitter.pop, on_error)
        except DeviceUnavailable:
            await _send_json(ws, {"type": "error", "reason": "device_unavailable"})
            device = None
            return

        try:
            await _send_json(ws, ready_message())

            async def receive() -> None:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    last_rx[0] = loop.time()
                    data = message.get("bytes")
                    if data is not None:
                        if len(data) == FRAME_BYTES:
                            jitter.push(data)
                        continue
                    try:
                        control = json.loads(message.get("text") or "")
                    except ValueError:
                        continue
                    if isinstance(control, dict) and control.get("type") == "stop":
                        return

            async def send_mic() -> None:
                while True:
                    await ws.send_bytes(await mic_queue.get())

            async def watch() -> str:
                interval = min(0.5, self._idle / 4)
                while True:
                    try:
                        return await asyncio.wait_for(events.get(), timeout=interval)
                    except asyncio.TimeoutError:
                        if loop.time() - last_rx[0] > self._idle:
                            return "idle_timeout"

            watcher = asyncio.create_task(watch())
            tasks = [asyncio.create_task(receive()), asyncio.create_task(send_mic()), watcher]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if watcher in done and not watcher.cancelled() and watcher.exception() is None:
                await _send_json(ws, {"type": "error", "reason": watcher.result()})
        finally:
            if device is not None:
                device.stop()


async def _send_json(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_json(payload)
    except (RuntimeError, OSError):
        pass


async def _close(ws: WebSocket) -> None:
    try:
        await ws.close()
    except (RuntimeError, OSError):
        pass
