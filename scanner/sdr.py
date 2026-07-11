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


@dataclass
class DetectedDevice:
    """One RTL-SDR visible on USB."""

    index: int
    serial: str
    manufacturer: str = ""
    product: str = ""

    def display(self) -> str:
        ser = self.serial or "(blank)"
        bits = [f"#{self.index}", f"serial={ser}"]
        if self.product:
            bits.append(self.product)
        if self.manufacturer:
            bits.append(self.manufacturer)
        return " ".join(bits)


def list_rtlsdr_devices() -> list[DetectedDevice]:
    """Enumerate connected RTL-SDR dongles (does not open a streaming handle)."""
    try:
        from rtlsdr import RtlSdr
    except ImportError as exc:
        raise SystemExit(
            "pyrtlsdr is not installed, or librtlsdr is missing.\n"
            "  pip install pyrtlsdr\n"
            "  sudo apt install librtlsdr-dev rtl-sdr"
        ) from exc

    devices: list[DetectedDevice] = []

    # Prefer high-level helper when present
    try:
        serials = RtlSdr.get_device_serial_addresses()
        if serials is not None:
            for i, ser in enumerate(serials):
                devices.append(DetectedDevice(index=i, serial=str(ser or "")))
            return devices
    except Exception:
        pass

    # librtlsdr C API
    try:
        from rtlsdr.rtlsdr import librtlsdr  # type: ignore

        n = int(librtlsdr.rtlsdr_get_device_count())
        for i in range(n):
            try:
                mfr = librtlsdr.rtlsdr_get_device_usb_strings(i)  # may not exist
            except Exception:
                mfr = None
            serial = ""
            manufacturer = ""
            product = ""
            try:
                # Some builds expose get_device_serial_addresses only; try usb strings
                import ctypes

                m = ctypes.create_string_buffer(256)
                p = ctypes.create_string_buffer(256)
                s = ctypes.create_string_buffer(256)
                if hasattr(librtlsdr, "rtlsdr_get_device_usb_strings"):
                    librtlsdr.rtlsdr_get_device_usb_strings(i, m, p, s)
                    manufacturer = m.value.decode("utf-8", "replace")
                    product = p.value.decode("utf-8", "replace")
                    serial = s.value.decode("utf-8", "replace")
            except Exception:
                pass
            if not serial:
                try:
                    serial = str(RtlSdr.get_device_serial_addresses()[i])
                except Exception:
                    serial = ""
            devices.append(
                DetectedDevice(
                    index=i,
                    serial=serial,
                    manufacturer=manufacturer,
                    product=product,
                )
            )
        return devices
    except Exception as exc:
        log.debug("Device enumeration via librtlsdr failed: %s", exc)

    # Last resort: try opening indices until failure
    for i in range(8):
        try:
            sdr = RtlSdr(device_index=i)
            ser = ""
            try:
                ser = str(getattr(sdr, "get_device_serial_addresses", lambda: [""])()[i])
            except Exception:
                pass
            try:
                sdr.close()
            except Exception:
                pass
            devices.append(DetectedDevice(index=i, serial=ser))
        except Exception:
            break
    return devices


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
        device_index: int | None = None,
        label: str = "",
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
        self.device_index = device_index
        self.label = label or (str(serial) if serial else f"idx{device_index}")
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
                kwargs: dict = {}
                if self.serial is not None and str(self.serial).strip() != "":
                    kwargs["serial_number"] = str(self.serial)
                elif self.device_index is not None:
                    kwargs["device_index"] = int(self.device_index)
                sdr = self._RtlSdr(**kwargs)
                sdr.sample_rate = self.sample_rate_hz
                if self.ppm_error:
                    try:
                        sdr.freq_correction = self.ppm_error
                    except Exception as exc:
                        log.warning(
                            "[%s] Could not set ppm_error=%s: %s",
                            self.label,
                            self.ppm_error,
                            exc,
                        )
                if self.gain and self.gain > 0:
                    sdr.gain = float(self.gain)
                else:
                    sdr.gain = "auto"
                self._sdr = sdr
                log.info(
                    "[%s] RTL-SDR ready: rate=%.3f Msps gain=%s ppm=%s serial=%s index=%s",
                    self.label,
                    float(sdr.sample_rate) / 1e6,
                    sdr.gain,
                    self.ppm_error,
                    self.serial or "auto",
                    self.device_index if self.device_index is not None else "auto",
                )
                return
            except Exception as exc:
                last = exc
                log.warning(
                    "[%s] SDR open attempt %d/%d failed: %s",
                    self.label,
                    i + 1,
                    attempts,
                    exc,
                )
                time.sleep(0.4 + 0.2 * i)
                try:
                    if self._sdr is not None:
                        self._sdr.close()
                except Exception:
                    pass
                self._sdr = None
        raise RuntimeError(
            f"[{self.label}] Could not open RTL-SDR after {attempts} attempts: {last}"
        )

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
        tname = f"rtl-iq-{self.label}"[:15]
        self._th = threading.Thread(target=self._reader_loop, name=tname, daemon=True)
        self._th.start()
        log.info(
            "[%s] IQ reader started (block_len=%d, qmax=%d)",
            self.label,
            self._block_len,
            self.queue_size,
        )

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
