from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal


def _design_channel_lpf(sample_rate: float, channel_bw_hz: float) -> tuple[np.ndarray, int]:
    """Return (sos, decim) for channel isolation."""
    target_rate = max(channel_bw_hz * 4.0, 48_000.0)
    decim = max(int(sample_rate // target_rate), 1)
    while decim > 1 and sample_rate / decim < channel_bw_hz * 2.5:
        decim -= 1
    nyq = sample_rate / 2.0
    cutoff = min(channel_bw_hz / 2.0, nyq * 0.45)
    sos = signal.butter(4, cutoff / nyq, btype="low", output="sos")
    return sos, decim


def extract_channel(
    iq: np.ndarray,
    sample_rate: float,
    center_freq: float,
    channel_freq: float,
    channel_bw_hz: float,
) -> tuple[np.ndarray, float]:
    """Stateless channel extract (one-shot). Prefer ChannelDemod for streaming."""
    dem = ChannelDemod(
        freq_hz=channel_freq,
        sample_rate=sample_rate,
        channel_bw_hz=channel_bw_hz,
        modulation="nfm",
        audio_rate=int(sample_rate),
        deemphasis_tau=None,
    )
    # Only shift+filter; return complex channel IQ path via public helper
    return dem.extract_only(iq, center_freq)


def fm_demod(iq: np.ndarray, prev: complex | None = None) -> tuple[np.ndarray, complex]:
    """Polar discriminator; optional prev sample for continuity across blocks."""
    if len(iq) == 0:
        return np.zeros(0, dtype=np.float32), prev if prev is not None else 0j
    if prev is not None and abs(prev) > 0:
        x = np.concatenate([[prev], iq])
        product = x[1:] * np.conj(x[:-1])
    else:
        product = iq[1:] * np.conj(iq[:-1])
        if len(product) == 0:
            return np.zeros(0, dtype=np.float32), complex(iq[-1])
    audio = np.angle(product).astype(np.float32)
    return audio, complex(iq[-1])


def am_demod(
    iq: np.ndarray,
    sample_rate: float = 48000.0,
    zi: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Envelope AM + mild HPF with optional filter state."""
    env = np.abs(iq).astype(np.float64)
    env = env - np.mean(env)
    zf = zi
    if len(env) > 16 and sample_rate > 0:
        cutoff = min(120.0, sample_rate * 0.02)
        wn = cutoff / (sample_rate / 2.0)
        if 0 < wn < 1:
            sos = signal.butter(2, wn, btype="highpass", output="sos")
            if zi is None:
                zi = signal.sosfilt_zi(sos) * 0.0
            env, zf = signal.sosfilt(sos, env, zi=zi)
    return env.astype(np.float32), zf


def deemphasis(audio: np.ndarray, sample_rate: float, tau: float, z: float = 0.0) -> tuple[np.ndarray, float]:
    if tau is None or tau <= 0 or len(audio) == 0:
        return audio.astype(np.float32), z
    alpha = float(np.exp(-1.0 / (tau * sample_rate)))
    b = np.array([1.0 - alpha], dtype=np.float64)
    a = np.array([1.0, -alpha], dtype=np.float64)
    y, zf = signal.lfilter(b, a, audio.astype(np.float64), zi=np.array([z]))
    return y.astype(np.float32), float(zf[0])


def resample_audio(audio: np.ndarray, in_rate: float, out_rate: int) -> np.ndarray:
    """Downsample audio; prefer cheap FIR decimate over full FFT resample."""
    if abs(in_rate - out_rate) < 1.0:
        return audio.astype(np.float32)
    if len(audio) < 8:
        return audio.astype(np.float32)
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
    if len(audio) == 0:
        return audio.astype(np.float32)
    x = np.asarray(audio, dtype=np.float64)
    m = float(np.max(np.abs(x)))
    if m < 1e-9:
        return np.zeros_like(audio, dtype=np.float32)
    return (x * (peak / m)).astype(np.float32)


@dataclass
class ChannelDemod:
    """
    Stateful channel demod: continuous NCO phase + filter memory across IQ blocks.
    Emits *raw* (pre-normalized) float audio at audio_rate.
    """

    freq_hz: float
    sample_rate: float
    channel_bw_hz: float
    modulation: str
    audio_rate: int
    deemphasis_tau: float | None = None

    phase: float = 0.0
    sos: np.ndarray | None = field(default=None, repr=False)
    zi: np.ndarray | None = field(default=None, repr=False)
    zi_real: np.ndarray | None = field(default=None, repr=False)
    zi_imag: np.ndarray | None = field(default=None, repr=False)
    decim: int = 1
    fm_prev: complex = 0j
    am_zi: np.ndarray | None = field(default=None, repr=False)
    deemp_z: float = 0.0
    _configured_sr: float = 0.0
    _configured_bw: float = 0.0

    def _ensure_filter(self, sample_rate: float, channel_bw_hz: float) -> None:
        if (
            self.sos is not None
            and abs(self._configured_sr - sample_rate) < 1.0
            and abs(self._configured_bw - channel_bw_hz) < 1.0
        ):
            return
        self.sos, self.decim = _design_channel_lpf(sample_rate, channel_bw_hz)
        self.zi = signal.sosfilt_zi(self.sos) * 0.0
        # complex path: sosfilt on real/imag separately shares structure; use complex as two reals
        # sosfilt_zi is for real; for complex we use two zi copies
        self.zi_real = self.zi.copy()
        self.zi_imag = self.zi.copy()
        self._configured_sr = sample_rate
        self._configured_bw = channel_bw_hz

    def extract_only(self, iq: np.ndarray, center_hz: float) -> tuple[np.ndarray, float]:
        """Shift+LPF+decimate without demod (testing / tools)."""
        self._ensure_filter(self.sample_rate, self.channel_bw_hz)
        assert self.sos is not None
        offset = self.freq_hz - center_hz
        n = len(iq)
        t = np.arange(n, dtype=np.float64) / self.sample_rate
        ph = self.phase + 2.0 * np.pi * offset * t
        self.phase = float((self.phase + 2.0 * np.pi * offset * n / self.sample_rate) % (2.0 * np.pi))
        shifted = iq * np.exp(-1j * ph).astype(np.complex64)
        re, self.zi_real = signal.sosfilt(self.sos, shifted.real, zi=self.zi_real)
        im, self.zi_imag = signal.sosfilt(self.sos, shifted.imag, zi=self.zi_imag)
        filtered = (re + 1j * im).astype(np.complex64)
        if self.decim > 1:
            filtered = filtered[:: self.decim]
        return filtered, self.sample_rate / self.decim

    def process(
        self,
        iq: np.ndarray,
        center_hz: float,
        sample_rate: float | None = None,
    ) -> np.ndarray:
        """Process one IQ block → raw float32 audio at audio_rate (not peak-normalized)."""
        sr = float(sample_rate if sample_rate is not None else self.sample_rate)
        self.sample_rate = sr
        self._ensure_filter(sr, self.channel_bw_hz)
        assert self.sos is not None

        offset = self.freq_hz - center_hz
        n = len(iq)
        if n == 0:
            return np.zeros(0, dtype=np.float32)

        t = np.arange(n, dtype=np.float64) / sr
        ph = self.phase + 2.0 * np.pi * offset * t
        self.phase = float((self.phase + 2.0 * np.pi * offset * n / sr) % (2.0 * np.pi))
        shifted = iq * np.exp(-1j * ph).astype(np.complex64)

        re, self.zi_real = signal.sosfilt(self.sos, shifted.real, zi=self.zi_real)
        im, self.zi_imag = signal.sosfilt(self.sos, shifted.imag, zi=self.zi_imag)
        filtered = (re + 1j * im).astype(np.complex64)
        if self.decim > 1:
            filtered = filtered[:: self.decim]
        ch_rate = sr / self.decim

        mod = self.modulation.lower()
        if mod in ("nfm", "fm", "wfm"):
            audio, self.fm_prev = fm_demod(filtered, self.fm_prev)
            if self.deemphasis_tau:
                audio, self.deemp_z = deemphasis(
                    audio, ch_rate, float(self.deemphasis_tau), self.deemp_z
                )
        elif mod == "am":
            audio, self.am_zi = am_demod(filtered, sample_rate=ch_rate, zi=self.am_zi)
            audio = audio - np.mean(audio)
        else:
            raise ValueError(f"unsupported modulation: {self.modulation}")

        return resample_audio(audio, ch_rate, self.audio_rate)


def demodulate(
    iq: np.ndarray,
    sample_rate: float,
    center_freq: float,
    channel_freq: float,
    channel_bw_hz: float,
    modulation: str,
    audio_rate: int,
    deemphasis_tau: float | None,
    normalize: bool = True,
) -> np.ndarray:
    """One-shot demod (stateless). Prefer ChannelDemod.process for streaming."""
    dem = ChannelDemod(
        freq_hz=channel_freq,
        sample_rate=sample_rate,
        channel_bw_hz=channel_bw_hz,
        modulation=modulation,
        audio_rate=audio_rate,
        deemphasis_tau=deemphasis_tau,
    )
    audio = dem.process(iq, center_freq, sample_rate)
    return normalize_audio(audio) if normalize else audio
