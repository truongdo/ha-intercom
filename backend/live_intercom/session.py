from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable

from starlette.websockets import WebSocket

from .audio.device import AudioDevice, DeviceFactory, DeviceUnavailable
from .audio.jitter import JitterBuffer
from .audio.ringtone import RingtoneSource
from .protocol import FRAME_BYTES, FRAME_MS, ready_message, rejected_message, ringing_message

log = logging.getLogger(__name__)

MIC_QUEUE_FRAMES = 50
STOP_TIMEOUT_S = 3.0
BYPASS_WINDOW_S = 300.0  # 5 minutes: how long a host-initiated call trigger stays "armed"


class SessionManager:
    def __init__(
        self,
        device_factory: DeviceFactory,
        jitter_ms: int,
        idle_timeout_s: float,
        pickup_mode_provider: Callable[[], str],
        ring_timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._device_factory = device_factory
        self._max_frames = max(1, jitter_ms // FRAME_MS)
        self._idle = idle_timeout_s
        self._pickup_mode_provider = pickup_mode_provider
        self._ring_timeout = ring_timeout_s
        self._clock = clock
        self._bypass_until: float | None = None
        self._active = False
        # Serialises device opening with the /api/me PortAudio refresh (see web.py).
        self.device_lock = asyncio.Lock()
        # Set only while a "confirm" mode call is ringing. confirm()/reject() are called from
        # an HTTP handler that may run on a different thread than this session's event loop
        # (true in the test harness; also a robustness guarantee in production), so they
        # resolve the pending event via call_soon_threadsafe rather than touching it directly.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_event: asyncio.Event | None = None
        self._pending_decision: str | None = None
        # Guards the check-and-set below: without it, two confirm()/reject() calls made back to
        # back (from a thread other than the session's event loop) can both observe
        # `_pending_event` still set and both return True, because clearing it happens later, on
        # the event loop thread, after call_soon_threadsafe's callback actually runs. Claiming the
        # decision synchronously here — not by waiting on the event loop — is what makes only the
        # first caller win.
        self._resolve_lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._active

    def confirm(self) -> bool:
        return self._resolve("confirm")

    def reject(self) -> bool:
        return self._resolve("declined")

    def _resolve(self, decision: str) -> bool:
        with self._resolve_lock:
            if self._pending_event is None or self._loop is None or self._pending_decision is not None:
                return False
            self._pending_decision = decision
            loop, event = self._loop, self._pending_event
        loop.call_soon_threadsafe(event.set)
        return True

    def trigger_bypass(self) -> None:
        self._bypass_until = self._clock() + BYPASS_WINDOW_S

    def _consume_bypass(self) -> bool:
        armed = self._bypass_until is not None and self._clock() < self._bypass_until
        self._bypass_until = None
        return armed

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
        self._loop = loop
        jitter = JitterBuffer(self._max_frames)
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_FRAMES)
        events: asyncio.Queue[str] = asyncio.Queue()
        last_rx = [loop.time()]
        ringtone = RingtoneSource()

        def offer(frame: bytes) -> None:
            if mic_queue.full():
                mic_queue.get_nowait()  # drop oldest: real-time audio prefers a glitch to delay
            mic_queue.put_nowait(frame)

        def on_mic(frame: bytes) -> None:
            loop.call_soon_threadsafe(offer, frame)

        def discard_mic(frame: bytes) -> None:
            pass  # the host's own mic is not routed anywhere while ringing

        def on_error(reason: str) -> None:
            loop.call_soon_threadsafe(events.put_nowait, reason)

        # Indirection so ringing and the live call can share one continuously-open PortAudio
        # stream: device.start() binds these two callables once, but what they point at is
        # swapped in place the instant a call is confirmed — no stream teardown, no pickup delay.
        pull_box: list[Callable[[], bytes]] = [ringtone.next_frame]
        mic_sink_box: list[Callable[[bytes], None]] = [discard_mic]

        def pull_indirect() -> bytes:
            return pull_box[0]()

        def on_mic_indirect(frame: bytes) -> None:
            mic_sink_box[0](frame)

        device: AudioDevice | None = None
        reason: str | None = None
        # PortAudio calls can block for seconds: keep them off the event loop.
        async with self.device_lock:
            try:
                device = await asyncio.to_thread(self._device_factory)
                await asyncio.to_thread(device.start, on_mic_indirect, pull_indirect, on_error)
            except DeviceUnavailable:
                reason = "device_unavailable"
            except Exception:
                log.exception("audio device setup failed")
                reason = "device_error"
        if reason is not None:
            if device is not None:
                await _stop_device(device)
            await _send_json(ws, {"type": "error", "reason": reason})
            self._loop = None
            return

        try:
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

            # Spans ring and live phases unchanged: a caller who cancels mid-ring is handled by
            # the exact same "stop" / disconnect path as one who hangs up mid-call.
            receive_task = asyncio.create_task(receive())

            pickup_mode = await asyncio.to_thread(self._pickup_mode_provider)
            if self._consume_bypass():
                pickup_mode = "auto"
            if pickup_mode == "confirm":
                await _send_json(ws, ringing_message())
                confirm_event = asyncio.Event()
                self._pending_decision = None
                self._pending_event = confirm_event
                timeout_task = asyncio.create_task(asyncio.sleep(self._ring_timeout))
                confirm_task = asyncio.create_task(confirm_event.wait())
                done, pending = await asyncio.wait(
                    {confirm_task, timeout_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
                )
                self._pending_event = None

                if receive_task in done:
                    # caller cancelled or disconnected while still ringing
                    for task in (confirm_task, timeout_task):
                        if task in pending:
                            task.cancel()
                    await asyncio.gather(confirm_task, timeout_task, return_exceptions=True)
                    return

                # confirm_task or timeout_task fired; receive_task is untouched here and stays
                # alive for the live phase below (on confirm), or is cancelled just below (on
                # reject/timeout) — see Step 4's note on why it must never be cancelled blindly.
                loser = timeout_task if confirm_task in done else confirm_task
                loser.cancel()
                await asyncio.gather(loser, return_exceptions=True)

                decision = self._pending_decision if confirm_task in done else "timeout"
                if decision != "confirm":
                    await _send_json(ws, rejected_message(decision))
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                    return

            # Live phase: either auto mode, or a confirmed "confirm" mode call.
            pull_box[0] = jitter.pop
            mic_sink_box[0] = on_mic
            last_rx[0] = loop.time()  # idle-timeout counts from here, not from ring start
            await _send_json(ws, ready_message())

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
            tasks = [receive_task, asyncio.create_task(send_mic()), watcher]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if watcher in done and not watcher.cancelled() and watcher.exception() is None:
                await _send_json(ws, {"type": "error", "reason": watcher.result()})
        finally:
            self._loop = None
            await _stop_device(device)


async def _stop_device(device: AudioDevice) -> None:
    """Stop off-loop and bounded, so a hung PortAudio call can never keep the session lock held."""
    try:
        await asyncio.wait_for(asyncio.to_thread(device.stop), timeout=STOP_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.error("audio device stop timed out after %.1fs", STOP_TIMEOUT_S)
    except Exception:
        log.exception("audio device stop failed")


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
