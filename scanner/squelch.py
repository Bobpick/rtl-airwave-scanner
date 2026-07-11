from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("squelch.json")

DEFAULTS: dict[str, float] = {
    "snr_threshold_db": 12.0,
    "min_voice_score": 0.25,
    "min_activity_ratio": 0.04,
    "min_dynamic_range_db": 4.0,
    "min_speech_band_ratio": 0.20,
    "min_audio_rms": 0.008,
}

LIMITS: dict[str, tuple[float, float]] = {
    "snr_threshold_db": (4.0, 35.0),
    "min_voice_score": (0.10, 0.95),
    "min_activity_ratio": (0.01, 0.50),
    "min_dynamic_range_db": (1.0, 20.0),
    "min_speech_band_ratio": (0.05, 0.80),
    "min_audio_rms": (0.002, 0.10),
}

# Live enables — ham is split by meters so operators can deselect
BOOL_DEFAULTS: dict[str, bool] = {
    "enable_atc": False,
    "enable_gmrs": True,
    "enable_murs": True,
    "enable_marine": True,
    "enable_other": True,
    # Ham by meters (General privileges cover all of these allocations)
    "enable_ham_10m": True,
    "enable_ham_6m": True,
    "enable_ham_2m": True,
    "enable_ham_1p25m": True,
    "enable_ham_70cm": True,
    "enable_ham_33cm": True,
    "enable_ham_23cm": True,
    # Legacy master key (UI optional): if present and False, disables all ham_* 
    "enable_ham": True,
}

GROUP_KEYS = {
    "atc": "enable_atc",
    "gmrs": "enable_gmrs",
    "murs": "enable_murs",
    "marine": "enable_marine",
    "other": "enable_other",
    "ham_10m": "enable_ham_10m",
    "ham_6m": "enable_ham_6m",
    "ham_2m": "enable_ham_2m",
    "ham_1p25m": "enable_ham_1p25m",
    "ham_70cm": "enable_ham_70cm",
    "ham_33cm": "enable_ham_33cm",
    "ham_23cm": "enable_ham_23cm",
    # legacy umbrella
    "ham": "enable_ham",
}

HAM_METER_KEYS = [
    "enable_ham_10m",
    "enable_ham_6m",
    "enable_ham_2m",
    "enable_ham_1p25m",
    "enable_ham_70cm",
    "enable_ham_33cm",
    "enable_ham_23cm",
]


def clamp(key: str, value: float) -> float:
    lo, hi = LIMITS.get(key, (value, value))
    return float(max(lo, min(hi, value)))


def load_squelch(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    path = Path(path)
    data: dict[str, Any] = {**DEFAULTS, **BOOL_DEFAULTS}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in DEFAULTS:
                    if k in raw:
                        data[k] = clamp(k, float(raw[k]))
                for k in BOOL_DEFAULTS:
                    if k in raw:
                        data[k] = bool(raw[k])
                # Migrate old single enable_ham → all meters
                if "enable_ham" in raw and not any(k in raw for k in HAM_METER_KEYS):
                    for k in HAM_METER_KEYS:
                        data[k] = bool(raw["enable_ham"])
        except Exception as exc:
            log.warning("Could not read %s: %s", path, exc)
    return data


def save_squelch(settings: dict[str, Any], path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    path = Path(path)
    out: dict[str, Any] = {**DEFAULTS, **BOOL_DEFAULTS}
    for k in DEFAULTS:
        if k in settings:
            out[k] = clamp(k, float(settings[k]))
    for k in BOOL_DEFAULTS:
        if k in settings:
            out[k] = bool(settings[k])
    # Keep master ham flag in sync with any meter enabled
    out["enable_ham"] = any(out.get(k, False) for k in HAM_METER_KEYS)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def apply_to_config(cfg: Any, settings: dict[str, Any]) -> None:
    cfg.snr_threshold_db = float(settings["snr_threshold_db"])
    cfg.min_voice_score = float(settings["min_voice_score"])
    cfg.min_activity_ratio = float(settings["min_activity_ratio"])
    cfg.min_dynamic_range_db = float(settings["min_dynamic_range_db"])
    cfg.min_speech_band_ratio = float(settings["min_speech_band_ratio"])
    cfg.min_audio_rms = float(settings["min_audio_rms"])
    for k in BOOL_DEFAULTS:
        setattr(cfg, k, bool(settings.get(k, BOOL_DEFAULTS[k])))


def group_enabled(cfg: Any, group: str) -> bool:
    g = group.lower()
    # Legacy "ham" window names
    if g == "ham":
        return bool(getattr(cfg, "enable_ham", True))
    key = GROUP_KEYS.get(g, "enable_other")
    # Ham meters also require master enable_ham (if set false, all off)
    if key in HAM_METER_KEYS and not getattr(cfg, "enable_ham", True):
        return False
    return bool(getattr(cfg, key, True))
