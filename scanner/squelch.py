from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("squelch.json")

# Live-tunable RF / audio gates
DEFAULTS: dict[str, float] = {
    "snr_threshold_db": 12.0,
    "min_voice_score": 0.25,  # ≤0.30 enables loose audio accept
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

# Live band-group enables (hot-reloaded with squelch.json)
# ATC defaults OFF so ham/GMRS aren't drowned out; turn on from the UI when needed.
BOOL_DEFAULTS: dict[str, bool] = {
    "enable_atc": False,
    "enable_ham": True,
    "enable_gmrs": True,
    "enable_murs": True,
    "enable_marine": True,
    "enable_other": True,
}

GROUP_KEYS = {
    "atc": "enable_atc",
    "ham": "enable_ham",
    "gmrs": "enable_gmrs",
    "murs": "enable_murs",
    "marine": "enable_marine",
    "other": "enable_other",
}


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
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def apply_to_config(cfg: Any, settings: dict[str, Any]) -> None:
    """Mutate Config fields from live settings."""
    cfg.snr_threshold_db = float(settings["snr_threshold_db"])
    cfg.min_voice_score = float(settings["min_voice_score"])
    cfg.min_activity_ratio = float(settings["min_activity_ratio"])
    cfg.min_dynamic_range_db = float(settings["min_dynamic_range_db"])
    cfg.min_speech_band_ratio = float(settings["min_speech_band_ratio"])
    cfg.min_audio_rms = float(settings["min_audio_rms"])
    # Band enables stored on cfg for scanner filtering
    cfg.enable_atc = bool(settings.get("enable_atc", False))
    cfg.enable_ham = bool(settings.get("enable_ham", True))
    cfg.enable_gmrs = bool(settings.get("enable_gmrs", True))
    cfg.enable_murs = bool(settings.get("enable_murs", True))
    cfg.enable_marine = bool(settings.get("enable_marine", True))
    cfg.enable_other = bool(settings.get("enable_other", True))


def group_enabled(cfg: Any, group: str) -> bool:
    key = GROUP_KEYS.get(group.lower(), "enable_other")
    return bool(getattr(cfg, key, True))
