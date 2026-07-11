"""Post-demod speech cleanup: band-pass + light noise gate."""

from __future__ import annotations

import io
import logging

import numpy as np
from scipy import signal

log = logging.getLogger(__name__)


def speech_bandpass(
    audio: np.ndarray,
    sample_rate: float,
    *,
    highpass_hz: float = 300.0,
    lowpass_hz: float = 3400.0,
    order: int = 2,
) -> np.ndarray:
    """
    Band-pass around telephone-band speech to cut rumble and hiss.

    Default ~300–3400 Hz (narrow radio voice). Safe no-op if rates are silly.
    """
    if audio is None or len(audio) < 16 or sample_rate <= 0:
        return np.asarray(audio if audio is not None else [], dtype=np.float32)

    x = np.asarray(audio, dtype=np.float64)
    nyq = sample_rate / 2.0
    lo = float(highpass_hz)
    hi = float(lowpass_hz)
    if lo <= 0 and hi <= 0:
        return x.astype(np.float32)
    if hi <= 0 or hi >= nyq * 0.98:
        # high-pass only
        if lo <= 0 or lo >= nyq * 0.9:
            return x.astype(np.float32)
        wn = lo / nyq
        sos = signal.butter(order, wn, btype="highpass", output="sos")
        y = signal.sosfiltfilt(sos, x)
        return y.astype(np.float32)
    if lo <= 0:
        wn = min(hi / nyq, 0.99)
        sos = signal.butter(order, wn, btype="lowpass", output="sos")
        y = signal.sosfiltfilt(sos, x)
        return y.astype(np.float32)

    lo = max(lo, 20.0)
    hi = min(hi, nyq * 0.95)
    if lo >= hi:
        return x.astype(np.float32)
    sos = signal.butter(order, [lo / nyq, hi / nyq], btype="bandpass", output="sos")
    # filtfilt = zero-phase, fine for offline clips (not streaming state)
    try:
        y = signal.sosfiltfilt(sos, x)
    except ValueError:
        # clip shorter than padlen
        y = signal.sosfilt(sos, x)
    return y.astype(np.float32)


def noise_gate(
    audio: np.ndarray,
    sample_rate: float,
    *,
    frame_ms: float = 20.0,
    open_ratio: float = 0.35,
    floor: float = 0.02,
) -> np.ndarray:
    """
    Soft gate: attenuate frames well below the clip's active level.

    Keeps speech; reduces hiss between syllables. Not a full NR algorithm.
    """
    if audio is None or len(audio) < 32 or sample_rate <= 0:
        return np.asarray(audio if audio is not None else [], dtype=np.float32)

    x = np.asarray(audio, dtype=np.float64)
    frame = max(int(sample_rate * frame_ms / 1000.0), 8)
    n = (len(x) // frame) * frame
    if n < frame * 4:
        return x.astype(np.float32)

    frames = x[:n].reshape(-1, frame)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-20)
    # Threshold from quieter percentiles so steady hiss is gated
    thr = max(float(np.percentile(rms, 40)) * open_ratio, floor * float(np.percentile(rms, 90) + 1e-9))
    thr = max(thr, 1e-6)
    # Smooth open/close envelope
    open_env = (rms > thr).astype(np.float64)
    # mild attack/release via recursive smooth
    alpha_up, alpha_dn = 0.45, 0.12
    g = np.zeros_like(open_env)
    state = 0.0
    for i, o in enumerate(open_env):
        target = 1.0 if o > 0.5 else floor
        a = alpha_up if target > state else alpha_dn
        state = state + a * (target - state)
        g[i] = state
    # expand to samples
    gain = np.repeat(g, frame)
    y = x[:n] * gain
    if len(x) > n:
        y = np.concatenate([y, x[n:] * state])
    return y.astype(np.float32)


def enhance_speech(
    audio: np.ndarray,
    sample_rate: float,
    *,
    enabled: bool = True,
    highpass_hz: float = 300.0,
    lowpass_hz: float = 3400.0,
    gate: bool = True,
    renorm_peak: float = 0.9,
) -> np.ndarray:
    """
    Practical voice cleanup: band-pass → optional gate → peak re-normalize.

    Quality scoring should run on *pre-enhance* audio so filters do not
    inflate silence into “speech.”
    """
    if not enabled or audio is None or len(audio) == 0:
        return np.asarray(audio if audio is not None else [], dtype=np.float32)

    y = speech_bandpass(
        audio,
        sample_rate,
        highpass_hz=highpass_hz,
        lowpass_hz=lowpass_hz,
    )
    if gate:
        y = noise_gate(y, sample_rate)
    # Re-peak so listening level stays consistent after filtering
    m = float(np.max(np.abs(y))) if len(y) else 0.0
    if m > 1e-9 and renorm_peak > 0:
        y = (y * (renorm_peak / m)).astype(np.float32)
    return y.astype(np.float32)


def enhance_wav_bytes(
    data: bytes,
    *,
    enabled: bool = True,
    highpass_hz: float = 300.0,
    lowpass_hz: float = 3400.0,
    gate: bool = True,
) -> bytes | None:
    """Load WAV bytes, enhance, return new WAV bytes (PCM_16). None on failure."""
    if not enabled or not data:
        return data
    try:
        import soundfile as sf

        audio, sr = sf.read(io.BytesIO(data), always_2d=False)
        if getattr(audio, "ndim", 1) > 1:
            audio = np.mean(audio, axis=1)
        y = enhance_speech(
            np.asarray(audio, dtype=np.float32),
            float(sr),
            enabled=True,
            highpass_hz=highpass_hz,
            lowpass_hz=lowpass_hz,
            gate=gate,
        )
        out = io.BytesIO()
        sf.write(out, y, int(sr), subtype="PCM_16", format="WAV")
        return out.getvalue()
    except Exception as e:
        log.debug("enhance_wav_bytes failed: %s", e)
        return data
