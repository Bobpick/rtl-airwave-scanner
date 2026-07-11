from __future__ import annotations

import logging
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


class RtlSdrSource:
    """Thin wrapper around pyrtlsdr."""

    def __init__(
        self,
        sample_rate_hz: int,
        gain: float = 0.0,
        ppm_error: int = 0,
        serial: Optional[str] = None,
    ) -> None:
        try:
            from rtlsdr import RtlSdr
        except ImportError as exc:
            raise SystemExit(
                "pyrtlsdr is not installed, or librtlsdr is missing.\n"
                "  pip install pyrtlsdr\n"
                "  sudo apt install librtlsdr-dev rtl-sdr"
            ) from exc

        kwargs = {}
        if serial is not None:
            kwargs["serial_number"] = str(serial)

        self._sdr = RtlSdr(**kwargs)
        self._sdr.sample_rate = sample_rate_hz
        # Some dongles reject 0 ppm; only apply a non-zero correction.
        if ppm_error:
            try:
                self._sdr.freq_correction = int(ppm_error)
            except Exception as exc:
                log.warning("Could not set ppm_error=%s: %s", ppm_error, exc)
        if gain and gain > 0:
            self._sdr.gain = float(gain)
        else:
            self._sdr.gain = "auto"

        log.info(
            "RTL-SDR ready: rate=%.3f Msps gain=%s ppm=%s",
            self._sdr.sample_rate / 1e6,
            self._sdr.gain,
            ppm_error,
        )

    @property
    def sample_rate(self) -> float:
        return float(self._sdr.sample_rate)

    @property
    def center_freq(self) -> float:
        return float(self._sdr.center_freq)

    def set_center(self, hz: float) -> None:
        self._sdr.center_freq = float(hz)
        log.debug("Tuned to %.3f MHz", hz / 1e6)

    def read_samples(self, n: int) -> np.ndarray:
        """Return complex64 IQ samples, length n."""
        return np.asarray(self._sdr.read_samples(n), dtype=np.complex64)

    def close(self) -> None:
        try:
            self._sdr.close()
        except Exception:
            pass
