from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from ..config import AudioConfig
from ..protocol import FRAME_MS, RATE
from .device import DeviceUnavailable
from .echo import EchoCanceller, make_canceller
from .resample import resample, to_channels, to_mono

log = logging.getLogger(__name__)

CANDIDATE_RATES = (16000, 48000, 44100, 32000, 24000, 8000)


def pick_device(devices: list[dict], match: str) -> int:
    needle = match.lower()
    for index, info in enumerate(devices):
        if (
            needle in info["name"].lower()
            and info["max_input_channels"] > 0
            and info["max_output_channels"] > 0
        ):
            return index
    raise DeviceUnavailable(f"no duplex audio device matching {match!r}")


def choose_rate(supports: Callable[[int], bool], candidates=CANDIDATE_RATES) -> int:
    for rate in candidates:
        if supports(rate):
            return rate
    raise DeviceUnavailable("device supports none of the candidate sample rates")


def _refresh_portaudio(sd) -> None:
    # PortAudio caches the device list at init; re-init to see hot-plugged devices.
    # Only call when no stream is open.
    sd._terminate()
    sd._initialize()


def _supports(sd, index: int, rate: int, in_ch: int, out_ch: int) -> bool:
    try:
        sd.check_input_settings(device=index, channels=in_ch, dtype="int16", samplerate=rate)
        sd.check_output_settings(device=index, channels=out_ch, dtype="int16", samplerate=rate)
        return True
    except sd.PortAudioError:
        return False


class AlsaDevice:
    def __init__(self, index: int, rate: int, in_ch: int, out_ch: int, canceller: EchoCanceller):
        self._index, self._rate = index, rate
        self._in, self._out = in_ch, out_ch
        self._canceller = canceller
        self._block = rate * FRAME_MS // 1000
        self._stream = None
        self._stopping = False

    @property
    def rate(self) -> int:
        return self._rate

    def start(self, on_mic_frame, pull_speaker_frame, on_error) -> None:
        import sounddevice as sd

        self._on_mic, self._pull, self._on_error = on_mic_frame, pull_speaker_frame, on_error
        self._stopping = False
        try:
            self._stream = sd.RawStream(
                samplerate=self._rate,
                blocksize=self._block,
                device=(self._index, self._index),
                channels=(self._in, self._out),
                dtype="int16",
                callback=self._callback,
                finished_callback=self._finished,
            )
            self._stream.start()
        except sd.PortAudioError as exc:
            self._stream = None
            raise DeviceUnavailable(str(exc)) from exc

    def stop(self) -> None:
        self._stopping = True
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # device may already be gone
                log.exception("error while closing audio stream")

    def _finished(self) -> None:
        if not self._stopping:
            self._on_error("device_lost")

    def _callback(self, indata, outdata, frames, time_info, status) -> None:
        try:
            if status:
                log.debug("audio status: %s", status)
            mic = np.frombuffer(indata, dtype=np.int16).reshape(-1, self._in)
            mic16 = resample(to_mono(mic), self._rate, RATE)
            speaker = self._pull()
            cleaned = self._canceller.process(mic16.tobytes(), speaker)
            self._on_mic(cleaned)
            out = resample(np.frombuffer(speaker, dtype=np.int16), RATE, self._rate)
            outdata[:] = to_channels(out, self._out).tobytes()
        except Exception:
            log.exception("audio callback failed")
            outdata[:] = bytes(len(outdata))
            self._on_error("device_error")


def open_alsa_device(cfg: AudioConfig) -> AlsaDevice:
    import sounddevice as sd

    _refresh_portaudio(sd)  # the session manager guarantees no stream is open here
    devices = [dict(d) for d in sd.query_devices()]
    index = pick_device(devices, cfg.device_match)
    in_ch = min(devices[index]["max_input_channels"], 2)
    out_ch = min(devices[index]["max_output_channels"], 2)
    rate = choose_rate(lambda r: _supports(sd, index, r, in_ch, out_ch))
    log.info("using %s at %d Hz (%d in, %d out)", devices[index]["name"], rate, in_ch, out_ch)
    return AlsaDevice(index, rate, in_ch, out_ch, make_canceller(cfg.echo_cancel))


def probe_audio(cfg: AudioConfig, refresh: bool) -> bool:
    try:
        import sounddevice as sd

        if refresh:
            _refresh_portaudio(sd)
        pick_device([dict(d) for d in sd.query_devices()], cfg.device_match)
        return True
    except Exception:
        return False
