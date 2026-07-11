"""Shared live spectrum / status snapshot for the web UI."""

from __future__ import annotations

import json
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
        "center_mhz": center_hz / 1e6,
        "span_mhz": sample_rate / 1e6,
        "freqs_mhz": (np.asarray(freqs) / 1e6).round(4).tolist(),
        "power_db": disp.round(2).tolist(),
        "peaks": peaks or [],
        # Full hop roster so the UI does not look "stuck" on one band
        "plan": plan or [],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def read_live_state(path: Path | str = DEFAULT_PATH) -> dict[str, Any] | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
