from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Band:
    name: str
    start_hz: int
    stop_hz: int
    modulation: str = "nfm"
    channel_bw_hz: float | None = None
    # Group for UI toggles: atc, gmrs, murs, marine, ham_2m, ham_70cm, …
    group: str = "other"

    @property
    def center_hz(self) -> float:
        return (self.start_hz + self.stop_hz) / 2.0

    @property
    def width_hz(self) -> float:
        return float(self.stop_hz - self.start_hz)


def infer_band_group(name: str) -> str:
    """Map window name → UI toggle group (ham split by meters)."""
    n = name.lower()
    if n.startswith("atc") or "airband" in n or n.startswith("air_"):
        return "atc"
    if n.startswith("gmrs") or n.startswith("frs"):
        return "gmrs"
    if n.startswith("murs"):
        return "murs"
    if n.startswith("marine"):
        return "marine"
    if n.startswith("10m"):
        return "ham_10m"
    if n.startswith("6m"):
        return "ham_6m"
    if n.startswith("2m"):
        return "ham_2m"
    if n.startswith("1p25") or n.startswith("1.25"):
        return "ham_1p25m"
    if n.startswith("70cm") or "kd6vlr" in n:
        return "ham_70cm"
    if n.startswith("33cm"):
        return "ham_33cm"
    if n.startswith("23cm"):
        return "ham_23cm"
    if n.startswith("ham") or "repeater" in n:
        return "ham_2m"  # fallback
    return "other"


@dataclass
class KnownChannel:
    name: str
    frequency_hz: float
    match_hz: float = 8000.0
    kind: str = "voice"
    notes: str = ""


@dataclass
class Config:
    sample_rate_hz: int = 2_048_000
    gain: float = 0.0
    ppm_error: int = 0
    serial: str | None = None
    fft_size: int = 4096
    averages: int = 8
    snr_threshold_db: float = 18.0
    min_peak_separation_hz: float = 12_500
    channel_bw_hz: float = 12_500
    edge_guard_fraction: float = 0.08
    min_active_seconds: float = 0.6
    hang_time_seconds: float = 2.0
    max_recording_seconds: float = 90.0
    spur_learn_seconds: float = 45.0
    spur_duty_threshold: float = 0.55
    ignored_frequencies_hz: list[float] = field(default_factory=list)
    ignore_bandwidth_hz: float = 15_000
    channel_step_hz: float = 25_000
    require_audio_quality: bool = True
    min_audio_rms: float = 0.015
    min_dynamic_range_db: float = 7.0
    min_activity_ratio: float = 0.08
    min_voice_score: float = 0.40
    min_speech_band_ratio: float = 0.35
    keep_rejected_audio: bool = False
    log_rejected: bool = False
    audio_sample_rate_hz: int = 16_000
    deemphasis_tau: float | None = 75e-6
    bands: list[Band] = field(default_factory=list)
    known_channels: list[KnownChannel] = field(default_factory=list)
    output_dir: Path = Path("recordings")
    database: Path = Path("recordings/transmissions.db")
    csv_path: Path = Path("recordings/transmissions.csv")
    log_level: str = "INFO"
    viewer_host: str = "127.0.0.1"
    viewer_port: int = 8765
    squelch_file: Path = Path("squelch.json")
    survey_fft_size: int = 2048
    survey_averages: int = 2
    hop_after_idle_looks: int = 2
    include_all_gmrs_labels: bool = True
    # Live toggles (overridden by squelch.json)
    enable_atc: bool = False
    enable_ham: bool = True
    enable_gmrs: bool = True
    enable_murs: bool = True
    enable_marine: bool = True
    enable_other: bool = True
    enable_ham_10m: bool = True
    enable_ham_6m: bool = True
    enable_ham_2m: bool = True
    enable_ham_1p25m: bool = True
    enable_ham_70cm: bool = True
    enable_ham_33cm: bool = True
    enable_ham_23cm: bool = True
    operator_callsign: str = ""
    operator_class: str = ""

    def match_channel(self, frequency_hz: float) -> KnownChannel | None:
        best: KnownChannel | None = None
        best_dist = float("inf")
        for ch in self.known_channels:
            d = abs(frequency_hz - ch.frequency_hz)
            if d <= ch.match_hz and d < best_dist:
                best = ch
                best_dist = d
        return best

    def is_protected_frequency(self, frequency_hz: float) -> bool:
        return self.match_channel(frequency_hz) is not None

    @staticmethod
    def _parse_bands(items: list[Any]) -> list[Band]:
        bands: list[Band] = []
        for b in items or []:
            name = str(b["name"])
            group = str(b.get("group") or infer_band_group(name)).lower()
            bands.append(
                Band(
                    name=name,
                    start_hz=int(b["start_hz"]),
                    stop_hz=int(b["stop_hz"]),
                    modulation=str(b.get("modulation", "nfm")).lower(),
                    channel_bw_hz=float(b["channel_bw_hz"]) if b.get("channel_bw_hz") else None,
                    group=group,
                )
            )
        return bands

    @staticmethod
    def _parse_channels(items: list[Any]) -> list[KnownChannel]:
        return [
            KnownChannel(
                name=str(c["name"]),
                frequency_hz=float(c["frequency_hz"]),
                match_hz=float(c.get("match_hz", 8000)),
                kind=str(c.get("kind", "voice")),
                notes=str(c.get("notes", "")),
            )
            for c in items or []
        ]

    @classmethod
    def _merge_site(cls, base_path: Path, raw: dict[str, Any]) -> dict[str, Any]:
        site_name = raw.get("site_file", "site.yaml")
        site_path = Path(site_name)
        if not site_path.is_absolute():
            site_path = (base_path.parent / site_path).resolve()
        if not site_path.is_file():
            return raw

        with open(site_path, encoding="utf-8") as f:
            site: dict[str, Any] = yaml.safe_load(f) or {}

        raw = dict(raw)
        raw["known_channels"] = list(raw.get("known_channels") or []) + list(
            site.get("known_channels") or []
        )
        raw["bands"] = list(site.get("bands") or []) + list(raw.get("bands") or [])
        detection = dict(raw.get("detection") or {})
        ignored = list(detection.get("ignored_frequencies_hz") or [])
        ignored.extend(float(x) for x in (site.get("ignored_frequencies_hz") or []))
        detection["ignored_frequencies_hz"] = ignored
        raw["detection"] = detection
        if site.get("site_name"):
            raw["_site_name"] = site["site_name"]
        if site.get("operator_callsign"):
            raw["operator_callsign"] = site["operator_callsign"]
        if site.get("operator_class"):
            raw["operator_class"] = site["operator_class"]
        return raw

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        raw = cls._merge_site(path, raw)

        device = raw.get("device", {})
        detection = raw.get("detection", {})
        audio = raw.get("audio", {})
        output = raw.get("output", {})
        logging_cfg = raw.get("logging", {})
        viewer = raw.get("viewer", {})
        quality = raw.get("quality", {})
        scan = raw.get("scan", {})

        bands = cls._parse_bands(raw.get("bands", []))
        if not bands:
            raise ValueError("config must define at least one band under 'bands'")

        seen_names: set[str] = set()
        unique_bands: list[Band] = []
        for b in bands:
            if b.name in seen_names:
                continue
            seen_names.add(b.name)
            unique_bands.append(b)
        bands = unique_bands

        known = cls._parse_channels(raw.get("known_channels", []))
        include_gmrs = bool(scan.get("include_all_gmrs_labels", True))
        if include_gmrs:
            from scanner.gmrs import gmrs_known_channels

            existing = {round(ch.frequency_hz) for ch in known}
            for ch in gmrs_known_channels():
                if round(ch.frequency_hz) not in existing:
                    known.append(ch)
                    existing.add(round(ch.frequency_hz))

        ignored = [float(x) for x in detection.get("ignored_frequencies_hz", [])]

        cfg = cls(
            sample_rate_hz=int(device.get("sample_rate_hz", 2_048_000)),
            gain=float(device.get("gain", 0)),
            ppm_error=int(device.get("ppm_error", 0)),
            serial=device.get("serial"),
            fft_size=int(detection.get("fft_size", 4096)),
            averages=int(detection.get("averages", 8)),
            snr_threshold_db=float(detection.get("snr_threshold_db", 18.0)),
            min_peak_separation_hz=float(detection.get("min_peak_separation_hz", 12_500)),
            channel_bw_hz=float(detection.get("channel_bw_hz", 12_500)),
            edge_guard_fraction=float(detection.get("edge_guard_fraction", 0.08)),
            min_active_seconds=float(detection.get("min_active_seconds", 0.6)),
            hang_time_seconds=float(detection.get("hang_time_seconds", 2.0)),
            max_recording_seconds=float(detection.get("max_recording_seconds", 90)),
            spur_learn_seconds=float(detection.get("spur_learn_seconds", 45)),
            spur_duty_threshold=float(detection.get("spur_duty_threshold", 0.55)),
            ignored_frequencies_hz=ignored,
            ignore_bandwidth_hz=float(detection.get("ignore_bandwidth_hz", 15_000)),
            channel_step_hz=float(detection.get("channel_step_hz", 25_000)),
            require_audio_quality=bool(quality.get("require_audio_quality", True)),
            min_audio_rms=float(quality.get("min_audio_rms", 0.015)),
            min_dynamic_range_db=float(quality.get("min_dynamic_range_db", 7.0)),
            min_activity_ratio=float(quality.get("min_activity_ratio", 0.08)),
            min_voice_score=float(quality.get("min_voice_score", 0.40)),
            min_speech_band_ratio=float(quality.get("min_speech_band_ratio", 0.35)),
            keep_rejected_audio=bool(quality.get("keep_rejected_audio", False)),
            log_rejected=bool(quality.get("log_rejected", False)),
            audio_sample_rate_hz=int(audio.get("sample_rate_hz", 16_000)),
            deemphasis_tau=audio.get("deemphasis_tau", 75e-6),
            bands=bands,
            known_channels=known,
            output_dir=Path(output.get("directory", "recordings")),
            database=Path(output.get("database", "recordings/transmissions.db")),
            csv_path=Path(output.get("csv", "recordings/transmissions.csv")),
            log_level=str(logging_cfg.get("level", "INFO")).upper(),
            viewer_host=str(viewer.get("host", "127.0.0.1")),
            viewer_port=int(viewer.get("port", 8765)),
            squelch_file=Path(raw.get("squelch_file", "squelch.json")),
            survey_fft_size=int(scan.get("survey_fft_size", 2048)),
            survey_averages=int(scan.get("survey_averages", 2)),
            hop_after_idle_looks=int(scan.get("hop_after_idle_looks", 2)),
            include_all_gmrs_labels=include_gmrs,
            operator_callsign=str(raw.get("operator_callsign") or ""),
            operator_class=str(raw.get("operator_class") or ""),
        )
        site_name = raw.get("_site_name")
        if site_name:
            import logging

            logging.getLogger(__name__).info("Site overlay loaded: %s", site_name)
        if cfg.operator_callsign:
            import logging

            logging.getLogger(__name__).info(
                "Operator: %s %s",
                cfg.operator_callsign,
                cfg.operator_class or "",
            )
        return cfg
