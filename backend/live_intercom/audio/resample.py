from __future__ import annotations

import numpy as np


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return samples
    if src_rate % dst_rate == 0:
        factor = src_rate // dst_rate
        usable = len(samples) // factor * factor
        grouped = samples[:usable].astype(np.int32).reshape(-1, factor)
        return grouped.mean(axis=1).astype(np.int16)
    out_len = int(len(samples) * dst_rate / src_rate)
    positions = np.arange(out_len) * (src_rate / dst_rate)
    return np.rint(np.interp(positions, np.arange(len(samples)), samples)).astype(np.int16)


def to_mono(block: np.ndarray) -> np.ndarray:
    if block.shape[1] == 1:
        return block[:, 0]
    return block.astype(np.int32).mean(axis=1).astype(np.int16)


def to_channels(mono: np.ndarray, channels: int) -> np.ndarray:
    return np.repeat(mono.reshape(-1, 1), channels, axis=1)
