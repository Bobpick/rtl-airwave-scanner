"""Shared live spectrum / status snapshot for the web UI."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_PATH = Path("recordings/live_state.json")


def write_live_state(
    *,
    site_name: str,
    mode: str,
    gain: float,
    band_name: str,
    group: str,
    center_hz: float,
    sample_rate: float,
    freqs_hz: np.ndarray,
    power_db: np.ndarray,
    peaks: list[dict[str, Any]] | None = None,
    plan: list[dict[str, Any]] | None = None,
    radio: str | None = None,
    radios: list[dict[str, Any]] | None = None,
    path: Path | str = DEFAULT_PATH,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Downsample for the UI (~400 bins across the current window)
    n = len(power_db)
    target = 400
    if n > target:
        step = n // target
        idx = np.arange(0, n, step)[:target]
        freqs = freqs_hz[idx]
        power = power_db[idx]
    else:
        freqs = freqs_hz
        power = power_db

    # Convert to display scale: peak at ~0, noise near -80..-100 (mockup style)
    p = np.asarray(power, dtype=np.float64)
    pmax = float(np.percentile(p, 99))
    disp = p - pmax  # 0 at strong peaks
    disp = np.clip(disp, -100.0, 0.0)

    payload = {
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "ts": time.time(),
        "site": site_name or "LOCAL",
        "mode": mode,
        "gain_db": float(gain) if gain else 0.0,
        "band": band_name,
        "group": group,
        "radio": radio or "",
        "center_mhz": center_hz / 1e6,
        "span_mhz": sample_rate / 1e6,
        "freqs_mhz": (np.asarray(freqs) / 1e6).round(4).tolist(),
        "power_db": disp.round(2).tolist(),
        "peaks": peaks or [],
        # Full hop roster so the UI does not look "stuck" on one band
        "plan": plan or [],
        "radios": radios or [],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


class LiveHub:
    """
    Merge multi-dongle status into one live_state.json.
    Spectrum frame comes from the most recently updating radio (or preferred).
    """

    def __init__(self, path: Path | str, site_name: str = "LOCAL") -> None:
        self.path = Path(path)
        self.site_name = site_name
        self._lock = threading.Lock()
        self._radios: dict[str, dict[str, Any]] = {}
        self._spectrum: dict[str, Any] | None = None
        self._period_s = 0.15
        self._last_write = 0.0

    def publish(
        self,
        *,
        radio: str,
        mode: str,
        gain: float,
        band_name: str,
        group: str,
        center_hz: float,
        sample_rate: float,
        freqs_hz: np.ndarray | None = None,
        power_db: np.ndarray | None = None,
        peaks: list[dict[str, Any]] | None = None,
        plan: list[dict[str, Any]] | None = None,
        serial: str | None = None,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            self._radios[radio] = {
                "label": radio,
                "mode": mode,
                "gain_db": float(gain) if gain else 0.0,
                "band": band_name,
                "group": group,
                "center_mhz": center_hz / 1e6,
                "span_mhz": sample_rate / 1e6,
                "serial": serial or "",
                "peaks": (peaks or [])[:8],
                "ts": time.time(),
            }
            if freqs_hz is not None and power_db is not None:
                self._spectrum = {
                    "radio": radio,
                    "mode": mode,
                    "gain": gain,
                    "band_name": band_name,
                    "group": group,
                    "center_hz": center_hz,
                    "sample_rate": sample_rate,
                    "freqs_hz": freqs_hz,
                    "power_db": power_db,
                    "peaks": peaks or [],
                    "plan": plan or [],
                }
            if not force and (now - self._last_write) < self._period_s:
                return
            self._last_write = now
            self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        radios = sorted(self._radios.values(), key=lambda r: r.get("label", ""))
        if self._spectrum is None:
            # Status-only write
            payload = {
                "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
                "ts": time.time(),
                "site": self.site_name or "LOCAL",
                "mode": radios[0]["mode"] if radios else "IDLE",
                "gain_db": radios[0]["gain_db"] if radios else 0.0,
                "band": radios[0]["band"] if radios else "",
                "group": radios[0]["group"] if radios else "",
                "radio": radios[0]["label"] if radios else "",
                "center_mhz": radios[0]["center_mhz"] if radios else 0.0,
                "span_mhz": radios[0].get("span_mhz", 0.0) if radios else 0.0,
                "freqs_mhz": [],
                "power_db": [],
                "peaks": [],
                "plan": [],
                "radios": radios,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.path)
            return

        sp = self._spectrum
        write_live_state(
            site_name=self.site_name,
            mode=str(sp["mode"]),
            gain=float(sp["gain"]),
            band_name=str(sp["band_name"]),
            group=str(sp["group"]),
            center_hz=float(sp["center_hz"]),
            sample_rate=float(sp["sample_rate"]),
            freqs_hz=sp["freqs_hz"],
            power_db=sp["power_db"],
            peaks=sp.get("peaks"),
            plan=sp.get("plan"),
            radio=str(sp.get("radio") or ""),
            radios=radios,
            path=self.path,
        )


def read_live_state(path: Path | str = DEFAULT_PATH) -> dict[str, Any] | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
