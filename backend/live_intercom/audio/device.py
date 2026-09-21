from __future__ import annotations

from typing import Callable, Protocol


class DeviceUnavailable(Exception):
    """The audio device is missing or cannot be opened."""


class AudioDevice(Protocol):
    def start(
        self,
        on_mic_frame: Callable[[bytes], None],
        pull_speaker_frame: Callable[[], bytes],
        on_error: Callable[[str], None],
    ) -> None: ...

    def stop(self) -> None: ...


DeviceFactory = Callable[[], AudioDevice]
