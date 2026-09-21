from __future__ import annotations

import numpy as np


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return samples
    if src_rate % dst_rate == 0:
        factor = src_rate // dst_rate
        usable = len(samples) // factor * factor
        grouped = samples[:usable].astype(np.int32).reshape(-1, factor)
        return np.rint(grouped.mean(axis=1)).astype(np.int16)
    if src_rate > dst_rate and len(samples) > 1:
        samples = _box_lowpass(samples, src_rate / dst_rate)
    out_len = int(len(samples) * dst_rate / src_rate)
    positions = np.arange(out_len) * (src_rate / dst_rate)
    return np.rint(np.interp(positions, np.arange(len(samples)), samples)).astype(np.int16)


def to_mono(block: np.ndarray) -> np.ndarray:
    if block.shape[1] == 1:
        return block[:, 0]
    return block.astype(np.int32).mean(axis=1).astype(np.int16)


def to_channels(mono: np.ndarray, channels: int) -> np.ndarray:
    return np.repeat(mono.reshape(-1, 1), channels, axis=1)


def _box_lowpass(samples: np.ndarray, ratio: float) -> np.ndarray:
    """Moving average about ``ratio`` samples wide: a cheap anti-alias filter before decimating."""
    width = max(2, int(round(ratio)))
    padded = np.pad(samples.astype(np.float64), (width // 2, width - 1 - width // 2), mode="edge")
    return np.convolve(padded, np.ones(width) / width, mode="valid")
