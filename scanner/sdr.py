from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class IqBlock:
    """One contiguous IQ capture at a fixed center frequency."""

    iq: np.ndarray
    center_hz: float
    sample_rate: float
    t0: float
    dropped_before: int = 0  # blocks dropped due to full queue since last get


class RtlSdrSource:
    """
    RTL-SDR wrapper with a background reader thread.

    USB ingest runs continuously so DSP/demod on the main thread cannot
    starve librtlsdr. After retune, the queue is flushed and a short settle
    delay avoids LO/PLL garbage peaks.
    """

    def __init__(
        self,
        sample_rate_hz: int,
        gain: float = 0.0,
        ppm_error: int = 0,
        serial: Optional[str] = None,
        queue_size: int = 8,
        settle_ms: int = 20,
        open_attempts: int = 5,
    ) -> None:
        try:
            from rtlsdr import RtlSdr
        except ImportError as exc:
            raise SystemExit(
                "pyrtlsdr is not installed, or librtlsdr is missing.\n"
                "  pip install pyrtlsdr\n"
                "  sudo apt install librtlsdr-dev rtl-sdr"
            ) from exc

        self._RtlSdr = RtlSdr
        self.serial = serial
        self.sample_rate_hz = int(sample_rate_hz)
        self.gain = gain
        self.ppm_error = int(ppm_error)
        self.settle_ms = int(settle_ms)
        self.queue_size = int(queue_size)

        self._sdr = None
        self._q: queue.Queue[IqBlock | None] = queue.Queue(maxsize=self.queue_size)
        self._center = 0.0
        self._lock = threading.Lock()
        self._run = False
        self._th: threading.Thread | None = None
        self._block_len = 16384
        self._dropped = 0
        self._drop_lock = threading.Lock()

        self._open(open_attempts)

    def _open(self, attempts: int) -> None:
        last: Exception | None = None
        for i in range(max(1, attempts)):
            try:
                kwargs = {}
                if self.serial is not None:
                    kwargs["serial_number"] = str(self.serial)
                sdr = self._RtlSdr(**kwargs)
                sdr.sample_rate = self.sample_rate_hz
                if self.ppm_error:
                    try:
                        sdr.freq_correction = self.ppm_error
                    except Exception as exc:
                        log.warning("Could not set ppm_error=%s: %s", self.ppm_error, exc)
                if self.gain and self.gain > 0:
                    sdr.gain = float(self.gain)
                else:
                    sdr.gain = "auto"
                self._sdr = sdr
                log.info(
                    "RTL-SDR ready: rate=%.3f Msps gain=%s ppm=%s serial=%s",
                    float(sdr.sample_rate) / 1e6,
                    sdr.gain,
                    self.ppm_error,
                    self.serial or "auto",
                )
                return
            except Exception as exc:
                last = exc
                log.warning("SDR open attempt %d/%d failed: %s", i + 1, attempts, exc)
                time.sleep(0.4 + 0.2 * i)
                try:
                    if self._sdr is not None:
                        self._sdr.close()
                except Exception:
                    pass
                self._sdr = None
        raise RuntimeError(f"Could not open RTL-SDR after {attempts} attempts: {last}")

    def reopen(self) -> None:
        """Close and reopen device (USB unplug / BUSY recovery)."""
        self.stop()
        try:
            if self._sdr is not None:
                self._sdr.close()
        except Exception:
            pass
        self._sdr = None
        self._open(5)
        self.start(self._block_len)

    @property
    def sample_rate(self) -> float:
        assert self._sdr is not None
        return float(self._sdr.sample_rate)

    @property
    def center_freq(self) -> float:
        with self._lock:
            return self._center

    @property
    def gain_db(self) -> float:
        try:
            g = self._sdr.gain  # type: ignore[union-attr]
            return float(g) if not isinstance(g, str) else 0.0
        except Exception:
            return float(self.gain or 0)

    def start(self, block_len: int) -> None:
        if self._run:
            return
        self._block_len = max(int(block_len), 1024)
        self._run = True
        self._th = threading.Thread(target=self._reader_loop, name="rtl-iq", daemon=True)
        self._th.start()
        log.info("IQ reader thread started (block_len=%d, qmax=%d)", self._block_len, self.queue_size)

    def stop(self) -> None:
        self._run = False
        # Unblock get()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._th is not None and self._th.is_alive():
            self._th.join(timeout=2.0)
        self._th = None

    def set_center(self, hz: float, settle_ms: int | None = None) -> None:
        """Retune LO, flush stale IQ, wait for settle, discard one junk block."""
        assert self._sdr is not None
        settle = self.settle_ms if settle_ms is None else int(settle_ms)
        with self._lock:
            self._sdr.center_freq = float(hz)
            self._center = float(hz)
        # Drop backlog from previous center
        dropped = 0
        while True:
            try:
                self._q.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if settle > 0:
            time.sleep(settle / 1000.0)
        # Hardware flush: discard samples still in device pipeline
        try:
            with self._lock:
                _ = self._sdr.read_samples(min(4096, self._block_len))
        except Exception as exc:
            log.warning("post-tune flush failed: %s", exc)
        log.debug("Tuned to %.3f MHz (flushed %d queued blocks, settle=%dms)", hz / 1e6, dropped, settle)

    def set_block_len(self, n: int) -> None:
        """Adjust capture size (survey vs dwell). Applied on next read."""
        self._block_len = max(int(n), 1024)

    def get_block(self, timeout: float = 1.0) -> IqBlock | None:
        """Pop next IQ block. Returns None on timeout or stop sentinel."""
        try:
            item = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            return None
        with self._drop_lock:
            item.dropped_before = self._dropped
            self._dropped = 0
        return item

    def _reader_loop(self) -> None:
        assert self._sdr is not None
        while self._run:
            try:
                n = self._block_len
                with self._lock:
                    iq = np.asarray(self._sdr.read_samples(n), dtype=np.complex64)
                    center = self._center
                    sr = float(self._sdr.sample_rate)
                if center <= 0:
                    # Not tuned yet — discard
                    continue
                block = IqBlock(iq=iq, center_hz=center, sample_rate=sr, t0=time.monotonic())
                try:
                    self._q.put(block, timeout=0.25)
                except queue.Full:
                    # Prefer fresh spectrum over growing latency
                    try:
                        self._q.get_nowait()
                        with self._drop_lock:
                            self._dropped += 1
                    except queue.Empty:
                        pass
                    try:
                        self._q.put_nowait(block)
                    except queue.Full:
                        with self._drop_lock:
                            self._dropped += 1
            except Exception as exc:
                if not self._run:
                    break
                log.error("SDR read error: %s", exc)
                time.sleep(0.15)

    def close(self) -> None:
        self.stop()
        try:
            if self._sdr is not None:
                self._sdr.close()
        except Exception:
            pass
        self._sdr = None
