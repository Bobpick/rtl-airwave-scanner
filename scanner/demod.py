from __future__ import annotations

import numpy as np
from scipy import signal


def extract_channel(
    iq: np.ndarray,
    sample_rate: float,
    center_freq: float,
    channel_freq: float,
    channel_bw_hz: float,
) -> tuple[np.ndarray, float]:
    """Shift channel to baseband and low-pass filter. Returns (iq, new_rate)."""
    offset = channel_freq - center_freq
    t = np.arange(len(iq), dtype=np.float64) / sample_rate
    shifted = iq * np.exp(-1j * 2.0 * np.pi * offset * t).astype(np.complex64)

    # Decimate so channel_bw is a good fraction of new rate
    target_rate = max(channel_bw_hz * 4.0, 48_000.0)
    decim = max(int(sample_rate // target_rate), 1)
    while decim > 1 and sample_rate / decim < channel_bw_hz * 2.5:
        decim -= 1

    nyq = sample_rate / 2.0
    cutoff = min(channel_bw_hz / 2.0, nyq * 0.45)
    sos = signal.butter(4, cutoff / nyq, btype="low", output="sos")
    filtered = signal.sosfilt(sos, shifted).astype(np.complex64)

    if decim > 1:
        filtered = filtered[::decim]
    return filtered, sample_rate / decim


def fm_demod(iq: np.ndarray) -> np.ndarray:
    """Polar discriminator FM demodulation → float audio (pre-normalized)."""
    # x[n] * conj(x[n-1])
    product = iq[1:] * np.conj(iq[:-1])
    audio = np.angle(product).astype(np.float32)
    return audio


def am_demod(iq: np.ndarray, sample_rate: float = 48000.0) -> np.ndarray:
    """Envelope AM demodulation + gentle high-pass (keeps ATC voice intelligible)."""
    env = np.abs(iq).astype(np.float64)
    env = env - np.mean(env)
    # ~100 Hz high-pass (not a 1-sample differentiator, which kills speech)
    if len(env) > 16 and sample_rate > 0:
        cutoff = min(120.0, sample_rate * 0.02)
        wn = cutoff / (sample_rate / 2.0)
        if 0 < wn < 1:
            sos = signal.butter(2, wn, btype="highpass", output="sos")
            env = signal.sosfilt(sos, env)
    return env.astype(np.float32)


def deemphasis(audio: np.ndarray, sample_rate: float, tau: float) -> np.ndarray:
    """Single-pole de-emphasis filter H(s) = 1 / (1 + s*tau)."""
    if tau is None or tau <= 0:
        return audio
    alpha = float(np.exp(-1.0 / (tau * sample_rate)))
    b = np.array([1.0 - alpha], dtype=np.float64)
    a = np.array([1.0, -alpha], dtype=np.float64)
    return signal.lfilter(b, a, audio).astype(np.float32)


def resample_audio(audio: np.ndarray, in_rate: float, out_rate: int) -> np.ndarray:
    """Downsample audio; prefer cheap FIR decimate over full FFT resample."""
    if abs(in_rate - out_rate) < 1.0:
        return audio.astype(np.float32)
    if len(audio) < 8:
        return audio.astype(np.float32)
    # Integer decimation when rates allow (common: 48k→16k)
    if in_rate > out_rate:
        factor = int(round(in_rate / out_rate))
        if factor >= 2 and abs(in_rate / factor - out_rate) < 100.0:
            try:
                y = signal.decimate(audio, factor, ftype="fir", zero_phase=True)
                return np.asarray(y, dtype=np.float32)
            except Exception:
                pass
    n_out = int(round(len(audio) * out_rate / in_rate))
    if n_out < 1:
        return np.zeros(0, dtype=np.float32)
    return signal.resample(audio, n_out).astype(np.float32)


def normalize_audio(audio: np.ndarray, peak: float = 0.9) -> np.ndarray:
    """Peak normalize for listening. Weak signals get moderate boost, not zero."""
    if len(audio) == 0:
        return audio.astype(np.float32)
    x = np.asarray(audio, dtype=np.float64)
    m = float(np.max(np.abs(x)))
    if m < 1e-9:
        return np.zeros_like(audio, dtype=np.float32)
    return (x * (peak / m)).astype(np.float32)


def demodulate(
    iq: np.ndarray,
    sample_rate: float,
    center_freq: float,
    channel_freq: float,
    channel_bw_hz: float,
    modulation: str,
    audio_rate: int,
    deemphasis_tau: float | None,
) -> np.ndarray:
    channel_iq, ch_rate = extract_channel(
        iq, sample_rate, center_freq, channel_freq, channel_bw_hz
    )
    mod = modulation.lower()
    if mod in ("nfm", "fm", "wfm"):
        audio = fm_demod(channel_iq)
        if deemphasis_tau:
            audio = deemphasis(audio, ch_rate, float(deemphasis_tau))
    elif mod == "am":
        audio = am_demod(channel_iq, sample_rate=ch_rate)
        audio = audio - np.mean(audio)
    else:
        raise ValueError(f"unsupported modulation: {modulation}")

    audio = resample_audio(audio, ch_rate, audio_rate)
    return normalize_audio(audio)
