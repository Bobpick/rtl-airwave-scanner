from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Peak:
    frequency_hz: float
    power_db: float
    snr_db: float


def power_spectrum_db(
    iq: np.ndarray,
    sample_rate: float,
    center_freq: float,
    fft_size: int,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Welch-style averaged power spectrum in dB.
    Returns (freqs_hz, power_db) covering center_freq ± sample_rate/2.
    """
    n = len(iq)
    if n < fft_size:
        raise ValueError("not enough samples for FFT")

    window = np.hanning(fft_size).astype(np.float32)
    n_frames = n // fft_size
    acc = np.zeros(fft_size, dtype=np.float64)

    for i in range(n_frames):
        chunk = iq[i * fft_size : (i + 1) * fft_size]
        spectrum = np.fft.fftshift(np.fft.fft(chunk * window, n=fft_size))
        acc += (spectrum.real**2 + spectrum.imag**2)

    acc /= max(n_frames, 1)
    power_db = 10.0 * np.log10(acc + 1e-20)
    freqs = center_freq + np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / sample_rate))
    return freqs.astype(np.float64), power_db.astype(np.float64)


def estimate_noise_floor_db(power_db: np.ndarray) -> float:
    """Robust noise floor estimate (20th percentile)."""
    return float(np.percentile(power_db, 20))


def find_peaks(
    freqs_hz: np.ndarray,
    power_db: np.ndarray,
    snr_threshold_db: float,
    min_separation_hz: float,
    band_start_hz: float,
    band_stop_hz: float,
) -> list[Peak]:
    noise = estimate_noise_floor_db(power_db)
    threshold = noise + snr_threshold_db

    # Local maxima above threshold
    candidates: list[Peak] = []
    for i in range(1, len(power_db) - 1):
        if power_db[i] < threshold:
            continue
        if power_db[i] < power_db[i - 1] or power_db[i] < power_db[i + 1]:
            continue
        f = float(freqs_hz[i])
        if f < band_start_hz or f > band_stop_hz:
            continue
        candidates.append(
            Peak(
                frequency_hz=f,
                power_db=float(power_db[i]),
                snr_db=float(power_db[i] - noise),
            )
        )

    # Greedy non-max suppression by power
    candidates.sort(key=lambda p: p.power_db, reverse=True)
    selected: list[Peak] = []
    for peak in candidates:
        if any(abs(peak.frequency_hz - s.frequency_hz) < min_separation_hz for s in selected):
            continue
        selected.append(peak)

    selected.sort(key=lambda p: p.frequency_hz)
    return selected
