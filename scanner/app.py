from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from scanner.audio_quality import analyze_audio
from scanner.config import Band, Config
from scanner.audio_clean import enhance_speech
from scanner.demod import ChannelDemod, normalize_audio
from scanner.lockfile import acquire as acquire_lock
from scanner.live_state import LiveHub, write_live_state
from scanner.multi import RadioConfig, partition_bands, resolve_devices
from scanner.recorder import Transmission, TransmissionLog, utcnow
from scanner.retention import run_retention
from scanner.sdr import IqBlock, RtlSdrSource, list_rtlsdr_devices
from scanner.spectrum import find_peaks, power_spectrum_db
from scanner.squelch import apply_to_config, group_enabled, load_squelch

log = logging.getLogger(__name__)


@dataclass
class ActiveTrack:
    frequency_hz: float
    first_seen: float
    last_seen: float
    peak_snr_db: float
    snr_samples: list[float] = field(default_factory=list)
    audio_chunks: list[np.ndarray] = field(default_factory=list)  # raw (pre-norm)
    recording: bool = False
    start_time: object | None = None
    demod: ChannelDemod | None = None
    looks_above: int = 0  # consecutive looks meeting RF arm threshold


class ScannerApp:
    """
    One radio worker: owns an RtlSdrSource and hops its assigned band pool.

    Multi-dongle: run several ScannerApp instances (threads) sharing logger + LiveHub.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        radio: RadioConfig | None = None,
        band_pool: list[Band] | None = None,
        logger: TransmissionLog | None = None,
        live_hub: LiveHub | None = None,
        stop_event: threading.Event | None = None,
        run_retention_loop: bool = True,
        open_sdr: bool = True,
    ) -> None:
        self.cfg = cfg
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self.logger = logger or TransmissionLog(cfg.database, cfg.csv_path, cfg.output_dir)
        self.live_hub = live_hub
        self._run_retention_loop = run_retention_loop
        self.radio = radio or RadioConfig(
            label="R0",
            serial=cfg.serial,
            gain=cfg.gain,
            ppm_error=cfg.ppm_error,
        )
        self.radio_label = self.radio.label
        self.band_pool = list(band_pool) if band_pool is not None else list(cfg.bands)

        gain = float(self.radio.gain if self.radio.gain is not None else cfg.gain)
        ppm = int(self.radio.ppm_error if self.radio.ppm_error is not None else cfg.ppm_error)
        self.sdr: RtlSdrSource | None = None
        if open_sdr:
            self.sdr = RtlSdrSource(
                sample_rate_hz=cfg.sample_rate_hz,
                gain=gain,
                ppm_error=ppm,
                serial=self.radio.serial,
                device_index=self.radio.device_index,
                label=self.radio_label,
            )
        self.tracks: dict[int, ActiveTrack] = {}
        self.band_index = 0
        # Spur learning: key -> active look count / total looks this band session
        self._look_count = 0
        self._hit_counts: dict[int, int] = defaultdict(int)
        self._auto_ignored: set[int] = set()
        self._learn_started = time.monotonic()
        self._learning_done = False
        self._squelch_mtime: float | None = None
        self._last_squelch_check = 0.0
        self._site_name = "LOCAL"
        self._mode = "SURVEY"
        self._last_live_write = 0.0
        self._live_period_s = 0.15  # ~6–7 UI updates/s max
        self._last_retention = 0.0
        # Apply live squelch overrides if file already exists
        self._reload_squelch(force=True)
        # Optional site name from overlay
        try:
            import yaml

            site_path = Path(self.cfg.squelch_file).parent / "site.yaml"
            if not site_path.is_file():
                site_path = Path("site.yaml")
            if site_path.is_file():
                site = yaml.safe_load(site_path.read_text(encoding="utf-8")) or {}
                if site.get("site_name"):
                    self._site_name = str(site["site_name"])[:32]
        except Exception:
            pass
        if self.live_hub is not None:
            self.live_hub.site_name = self._site_name

    def request_stop(self, *_args) -> None:
        log.info("[%s] Stop requested…", self.radio_label)
        self._stop_event.set()

    @property
    def _stop(self) -> bool:
        return self._stop_event.is_set()

    def _maybe_run_retention(self, force: bool = False) -> None:
        """Zip WAVs past zip_after_hours; delete archives past delete_after_hours."""
        if not self._run_retention_loop:
            return
        interval = float(getattr(self.cfg, "retention_interval_seconds", 900.0) or 900.0)
        now = time.monotonic()
        if not force and (now - self._last_retention) < interval:
            return
        self._last_retention = now
        try:
            run_retention(
                self.cfg.output_dir,
                zip_after_hours=float(getattr(self.cfg, "audio_zip_after_hours", 12.0)),
                delete_after_hours=float(getattr(self.cfg, "audio_delete_after_hours", 72.0)),
                db_path=self.cfg.database,
            )
        except Exception as e:
            log.warning("Retention pass failed: %s", e)

    def _reload_squelch(self, force: bool = False) -> None:
        """Hot-reload squelch.json from the web UI (throttled)."""
        now = time.monotonic()
        if not force and now - self._last_squelch_check < 1.0:
            return
        self._last_squelch_check = now
        path = Path(self.cfg.squelch_file)
        try:
            mtime = path.stat().st_mtime if path.is_file() else None
        except OSError:
            mtime = None
        if not force and mtime == self._squelch_mtime:
            return
        self._squelch_mtime = mtime
        settings = load_squelch(path)
        apply_to_config(self.cfg, settings)
        groups = []
        for g, flag in (
            ("ATC", self.cfg.enable_atc),
            ("10m", self.cfg.enable_ham_10m),
            ("6m", self.cfg.enable_ham_6m),
            ("2m", self.cfg.enable_ham_2m),
            ("1.25m", self.cfg.enable_ham_1p25m),
            ("70cm", self.cfg.enable_ham_70cm),
            ("33cm", self.cfg.enable_ham_33cm),
            ("23cm", self.cfg.enable_ham_23cm),
            ("GMRS", self.cfg.enable_gmrs),
            ("MURS", self.cfg.enable_murs),
            ("marine", self.cfg.enable_marine),
        ):
            groups.append(f"{g}{'✓' if flag else '✗'}")
        log.info(
            "Squelch: RF≥%.1f dB voice≥%.2f act≥%.0f%% · bands %s",
            self.cfg.snr_threshold_db,
            self.cfg.min_voice_score,
            self.cfg.min_activity_ratio * 100,
            " ".join(groups),
        )

    def _quantize(self, freq_hz: float, band: Band | None = None) -> int:
        """Snap to channel grid. AM airband uses 25 kHz; NFM uses configured step."""
        if band is not None and band.modulation == "am":
            step = 25_000  # civil airband channel spacing (not 12.5)
        else:
            step = self.cfg.channel_step_hz or self.cfg.min_peak_separation_hz
        step = max(int(step), 1)
        return int(round(freq_hz / step) * step)

    def _is_ignored(self, freq_hz: float) -> bool:
        bw = self.cfg.ignore_bandwidth_hz
        for f in self.cfg.ignored_frequencies_hz:
            if abs(freq_hz - f) <= bw:
                return True
        q = self._quantize(freq_hz)
        if q in self._auto_ignored:
            return True
        return False

    def _survey_mode(self) -> bool:
        """Fast wideband survey when not recording anything."""
        return not any(t.recording for t in self.tracks.values())

    def _fft_params(self) -> tuple[int, int]:
        if self._survey_mode():
            return self.cfg.survey_fft_size, self.cfg.survey_averages
        return self.cfg.fft_size, self.cfg.averages

    def _samples_per_look(self) -> int:
        fft_size, averages = self._fft_params()
        return fft_size * averages

    def _channel_bw(self, band: Band) -> float:
        return float(band.channel_bw_hz or self.cfg.channel_bw_hz)

    def _tune_band(self, band: Band) -> float:
        assert self.sdr is not None
        sr = self.cfg.sample_rate_hz
        usable = sr * 0.9
        if band.width_hz > usable:
            log.warning(
                "[%s] Band %s width %.1f kHz exceeds ~%.1f kHz usable span; "
                "only center portion will be monitored",
                self.radio_label,
                band.name,
                band.width_hz / 1e3,
                usable / 1e3,
            )
        center = band.center_hz
        # Match reader block size to survey/dwell before retune
        self.sdr.set_block_len(self._samples_per_look())
        self.sdr.set_center(center)  # settle + flush handled in sdr.py
        # Reset spur learning per band hop
        self._look_count = 0
        self._hit_counts.clear()
        self._auto_ignored.clear()
        self._learn_started = time.monotonic()
        self._learning_done = False
        return center

    def _process_block(self, band: Band, block: IqBlock):
        """FFT + peak detect on one IQ block (no USB I/O here)."""
        assert self.sdr is not None
        fft_size, _averages = self._fft_params()
        # Ensure we have at least one FFT frame
        iq = block.iq
        if len(iq) < fft_size:
            return [], iq, block.center_hz
        # Use configured fft size; if block is longer, power_spectrum averages frames
        center = block.center_hz
        sr = block.sample_rate
        freqs, power = power_spectrum_db(iq, sr, center, fft_size)
        guard = sr * self.cfg.edge_guard_fraction
        lo = max(band.start_hz, center - sr / 2 + guard)
        hi = min(band.stop_hz, center + sr / 2 - guard)
        snr = self.cfg.snr_threshold_db
        if self._survey_mode():
            snr = max(8.0, snr - 3.0)
        peaks = find_peaks(
            freqs,
            power,
            snr_threshold_db=snr,
            min_separation_hz=self.cfg.min_peak_separation_hz,
            band_start_hz=lo,
            band_stop_hz=hi,
        )
        peaks = [p for p in peaks if not self._is_ignored(p.frequency_hz)]

        # Throttled dashboard feed (do not stall DSP with disk every look)
        now = time.monotonic()
        if now - self._last_live_write >= self._live_period_s:
            self._last_live_write = now
            try:
                self._mode = (
                    "DWELL" if any(t.recording for t in self.tracks.values()) else "SURVEY"
                )
                plan = [
                    {
                        "name": b.name,
                        "group": b.group,
                        "start_mhz": b.start_hz / 1e6,
                        "stop_mhz": b.stop_hz / 1e6,
                        "active": b.name == band.name,
                        "radio": self.radio_label,
                    }
                    for b in self._active_bands()
                ]
                peak_dicts = [
                    {
                        "mhz": p.frequency_hz / 1e6,
                        "snr_db": p.snr_db,
                        "group": band.group,
                        "radio": self.radio_label,
                    }
                    for p in peaks[:12]
                ]
                if self.live_hub is not None:
                    self.live_hub.publish(
                        radio=self.radio_label,
                        mode=self._mode,
                        gain=self.sdr.gain_db,
                        band_name=band.name,
                        group=band.group,
                        center_hz=center,
                        sample_rate=sr,
                        freqs_hz=freqs,
                        power_db=power,
                        peaks=peak_dicts,
                        plan=plan,
                        serial=str(self.radio.serial or ""),
                    )
                else:
                    write_live_state(
                        site_name=self._site_name,
                        mode=self._mode,
                        gain=self.sdr.gain_db,
                        band_name=band.name,
                        group=band.group,
                        center_hz=center,
                        sample_rate=sr,
                        freqs_hz=freqs,
                        power_db=power,
                        peaks=peak_dicts,
                        plan=plan,
                        radio=self.radio_label,
                        path=self.cfg.output_dir / "live_state.json",
                    )
            except Exception:
                pass

        return peaks, iq, center

    def _update_spur_learning(self, peaks) -> None:
        self._look_count += 1
        for peak in peaks:
            self._hit_counts[self._quantize(peak.frequency_hz)] += 1

        elapsed = time.monotonic() - self._learn_started
        if self._learning_done or elapsed < self.cfg.spur_learn_seconds:
            return
        if self._look_count < 10:
            return

        self._learning_done = True
        thr = self.cfg.spur_duty_threshold
        for key, hits in self._hit_counts.items():
            duty = hits / self._look_count
            if duty < thr:
                continue
            # Never auto-blacklist published airport channels (AWOS loops, etc.)
            if self.cfg.is_protected_frequency(float(key)):
                ch = self.cfg.match_channel(float(key))
                log.info(
                    "Persistent energy on published channel %s (%.4f MHz) — keeping",
                    ch.name if ch else "?",
                    key / 1e6,
                )
                continue
            self._auto_ignored.add(key)
            log.warning(
                "Auto-ignore spur/carrier ~%.4f MHz (duty %.0f%% during learn)",
                key / 1e6,
                duty * 100,
            )
        if self._auto_ignored:
            log.info(
                "Spur learning done: ignoring %d persistent frequencies",
                len(self._auto_ignored),
            )
        else:
            log.info("Spur learning done: no persistent carriers found")

    def _rf_armed(self, track: ActiveTrack) -> bool:
        """Require sustained SNR before starting a recording (cuts hop noise / blips)."""
        thr = self.cfg.snr_threshold_db
        if not track.snr_samples:
            return False
        # Time on frequency
        if (track.last_seen - track.first_seen) < self.cfg.min_active_seconds:
            return False
        # Recent looks must stay near threshold (not a single spike)
        recent = track.snr_samples[-5:]
        if len(recent) < 3:
            return False
        if float(np.mean(recent)) < thr - 3.0:
            return False
        if float(np.min(recent)) < thr - 6.0:
            return False
        # Prefer several consecutive above-threshold looks
        if track.looks_above < 3:
            return False
        return True

    def _ensure_demod(self, track: ActiveTrack, band: Band) -> ChannelDemod:
        tau = None if band.modulation == "am" else self.cfg.deemphasis_tau
        if track.demod is None or abs(track.demod.freq_hz - track.frequency_hz) > 1.0:
            track.demod = ChannelDemod(
                freq_hz=track.frequency_hz,
                sample_rate=self.sdr.sample_rate,
                channel_bw_hz=self._channel_bw(band),
                modulation=band.modulation,
                audio_rate=self.cfg.audio_sample_rate_hz,
                deemphasis_tau=tau,
            )
        return track.demod

    def _update_tracks(self, band: Band, peaks, iq: np.ndarray, center: float, now: float) -> None:
        # During spur learning, still observe hits but do not start recordings
        learning = not self._learning_done
        seen_keys = set()
        thr = self.cfg.snr_threshold_db

        for peak in peaks:
            key = self._quantize(peak.frequency_hz, band)
            seen_keys.add(key)
            track = self.tracks.get(key)
            # Prefer published channel (e.g. 122.725); else 25 kHz AM / 12.5 kHz NFM grid
            ch_known = self.cfg.match_channel(peak.frequency_hz)
            channel_hz = float(ch_known.frequency_hz) if ch_known else float(key)
            if track is None:
                track = ActiveTrack(
                    frequency_hz=channel_hz,
                    first_seen=now,
                    last_seen=now,
                    peak_snr_db=peak.snr_db,
                    snr_samples=[peak.snr_db],
                    looks_above=1 if peak.snr_db >= thr - 3.0 else 0,
                )
                self.tracks[key] = track
                log.debug("New energy @ %.4f MHz snr=%.1f", channel_hz / 1e6, peak.snr_db)
            else:
                track.last_seen = now
                track.frequency_hz = channel_hz
                track.peak_snr_db = max(track.peak_snr_db, peak.snr_db)
                track.snr_samples.append(peak.snr_db)
                if len(track.snr_samples) > 40:
                    track.snr_samples = track.snr_samples[-40:]
                if peak.snr_db >= thr - 3.0:
                    track.looks_above += 1
                else:
                    track.looks_above = 0

            if learning:
                continue

            # RF arming: do not START on a single FFT spike
            if not track.recording and self._rf_armed(track):
                track.recording = True
                track.start_time = utcnow()
                track.demod = None  # fresh demod state at record start
                ch = self.cfg.match_channel(track.frequency_hz)
                label = f"  [{ch.name}]" if ch else ""
                mean_r = float(np.mean(track.snr_samples[-5:]))
                log.info(
                    "[%s] START  %.4f MHz  band=%s  snr=%.1f (mean5=%.1f) dB%s",
                    self.radio_label,
                    track.frequency_hz / 1e6,
                    band.name,
                    track.peak_snr_db,
                    mean_r,
                    label,
                )

            if track.recording:
                dem = self._ensure_demod(track, band)
                # Raw audio — normalize once at finalize
                assert self.sdr is not None
                audio = dem.process(iq, center, self.sdr.sample_rate)
                if len(audio):
                    track.audio_chunks.append(audio)

                if track.start_time is not None:
                    elapsed = (utcnow() - track.start_time).total_seconds()
                    if elapsed >= self.cfg.max_recording_seconds:
                        self._finalize_track(key, track, band, force=True)

        hang = self.cfg.hang_time_seconds
        for key, track in list(self.tracks.items()):
            if key in seen_keys:
                continue
            # Decay arm counter when peak disappears
            track.looks_above = 0
            if learning:
                if now - track.last_seen >= hang:
                    self.tracks.pop(key, None)
                continue
            if now - track.last_seen >= hang:
                self._finalize_track(key, track, band)

    def _finalize_track(self, key: int, track: ActiveTrack, band: Band, force: bool = False) -> None:
        self.tracks.pop(key, None)
        if not track.recording or track.start_time is None:
            return
        end = utcnow()
        duration = (end - track.start_time).total_seconds()
        # Hard floor: sub-4s clips are almost never useful intel (noise/kerchunk)
        min_save = float(getattr(self.cfg, "min_recording_seconds", 4.0) or 4.0)
        if duration < min_save:
            log.info(
                "REJECT %.4f MHz  duration=%.2fs < %.1fs (too_short)",
                track.frequency_hz / 1e6,
                duration,
                min_save,
            )
            return
        # Also honor legacy arm-time floor when longer than min_save is not the issue
        if duration < self.cfg.min_active_seconds:
            log.debug("Drop short blip @ %.4f MHz (%.2fs)", track.frequency_hz / 1e6, duration)
            return

        mean_snr = float(np.mean(track.snr_samples)) if track.snr_samples else track.peak_snr_db
        audio_raw = None
        audio_out = None
        metrics = None
        if track.audio_chunks:
            audio_raw = np.concatenate(track.audio_chunks)
            # Quality on *pre-normalize* audio so pure noise cannot be boosted into "speech"
            is_am = band.modulation == "am"
            loose = self.cfg.min_voice_score <= 0.30 or is_am
            raw_rms = float(np.sqrt(np.mean(np.square(audio_raw.astype(np.float64))))) if len(audio_raw) else 0.0
            metrics = analyze_audio(
                audio_raw,
                self.cfg.audio_sample_rate_hz,
                min_rms=self.cfg.min_audio_rms,
                min_dynamic_range_db=self.cfg.min_dynamic_range_db,
                min_activity_ratio=self.cfg.min_activity_ratio,
                min_voice_score=self.cfg.min_voice_score,
                min_speech_band_ratio=self.cfg.min_speech_band_ratio,
                loose=loose,
            )

            def _reject(m, tag: str):
                return type(m)(
                    rms=m.rms,
                    peak=m.peak,
                    dynamic_range_db=m.dynamic_range_db,
                    activity_ratio=m.activity_ratio,
                    spectral_flux=m.spectral_flux,
                    speech_band_ratio=m.speech_band_ratio,
                    voice_score=m.voice_score,
                    is_likely_signal=False,
                    reason=(m.reason + "," + tag).strip(","),
                )

            # Steady RF carrier (low SNR variance) with weak audio dynamics → reject
            # (also for AM — open-carrier ATC static often has rock-steady SNR)
            if (
                track.snr_samples
                and len(track.snr_samples) >= 6
                and metrics is not None
                and metrics.is_likely_signal
            ):
                snr_std = float(np.std(track.snr_samples))
                snr_thr = 0.65 if is_am else 0.5
                voice_thr = 0.55 if is_am else 0.5
                if snr_std < snr_thr and metrics.voice_score < voice_thr:
                    metrics = _reject(metrics, "steady_snr")
                # AM: very steady SNR + low activity = open squelch, not a call
                if (
                    is_am
                    and snr_std < 1.0
                    and metrics.activity_ratio < 0.08
                    and metrics.dynamic_range_db < 7.0
                ):
                    metrics = _reject(metrics, "am_open_carrier")

            # Very weak pre-norm RMS is almost always static after AGC
            # AM uses a slightly higher floor (weak carrier hiss after peak-norm)
            weak_floor = self.cfg.min_audio_rms * (0.28 if is_am else 0.35)
            if is_am:
                weak_floor = max(weak_floor, 0.0032)
            if (
                metrics is not None
                and metrics.is_likely_signal
                and raw_rms < weak_floor
            ):
                metrics = _reject(metrics, "weak_raw_rms")

            # Peak-normalize once, then speech band-pass / gate for listening
            # AM profile: narrower (≈400–2800 Hz) + harder gate
            audio_out = normalize_audio(audio_raw)
            if getattr(self.cfg, "speech_enhance", True):
                audio_out = enhance_speech(
                    audio_out,
                    self.cfg.audio_sample_rate_hz,
                    enabled=True,
                    highpass_hz=float(getattr(self.cfg, "speech_hp_hz", 300.0)),
                    lowpass_hz=float(getattr(self.cfg, "speech_lp_hz", 3400.0)),
                    gate=bool(getattr(self.cfg, "speech_gate", True)),
                    profile="am" if is_am else None,
                )

        quality = "accepted"
        reason = "ok"
        audio_path = None
        min_samples = int(self.cfg.audio_sample_rate_hz * 0.25)

        if metrics is None or audio_out is None:
            quality = "rejected"
            reason = "no_audio"
        elif len(audio_out) < min_samples:
            quality = "rejected"
            reason = "audio_too_short"
        elif self.cfg.require_audio_quality and not metrics.is_likely_signal:
            quality = "rejected"
            reason = metrics.reason
        else:
            audio_path = self.logger.save_audio(
                audio_out,
                self.cfg.audio_sample_rate_hz,
                track.frequency_hz,
                track.start_time,
            )

        if quality == "rejected":
            log.info(
                "REJECT %.4f MHz  voice=%.2f act=%.0f%% snr=%.1f (%s)",
                track.frequency_hz / 1e6,
                metrics.voice_score if metrics else 0.0,
                (metrics.activity_ratio * 100) if metrics else 0.0,
                track.peak_snr_db,
                reason,
            )
            if (
                self.cfg.keep_rejected_audio
                and audio_out is not None
                and len(audio_out) > min_samples
            ):
                audio_path = self.logger.save_audio(
                    audio_out,
                    self.cfg.audio_sample_rate_hz,
                    track.frequency_hz,
                    track.start_time,
                    suffix="_rejected",
                )
        else:
            log.info(
                "ACCEPT %.4f MHz  voice=%.2f act=%.0f%% snr=%.1f mean_snr=%.1f",
                track.frequency_hz / 1e6,
                metrics.voice_score if metrics else 0.0,
                (metrics.activity_ratio * 100) if metrics else 0.0,
                track.peak_snr_db,
                mean_snr,
            )

        ch = self.cfg.match_channel(track.frequency_hz)
        if ch:
            note = f"{ch.name} — {ch.notes}" if ch.notes else ch.name
        else:
            note = ""
        radio_tag = f"radio={self.radio_label}"
        note = f"{note} · {radio_tag}" if note else radio_tag

        # Skip clutter: do not log pure static unless debugging
        if quality == "rejected" and not self.cfg.log_rejected and not self.cfg.keep_rejected_audio:
            return

        tx = Transmission(
            start_utc=track.start_time,
            end_utc=end,
            frequency_hz=float(track.frequency_hz),
            band_name=band.name,
            modulation=band.modulation,
            peak_snr_db=track.peak_snr_db,
            mean_snr_db=mean_snr,
            audio_path=audio_path,
            duration_seconds=duration,
            audio_rms=metrics.rms if metrics else 0.0,
            audio_peak=metrics.peak if metrics else 0.0,
            dynamic_range_db=metrics.dynamic_range_db if metrics else 0.0,
            activity_ratio=metrics.activity_ratio if metrics else 0.0,
            voice_score=metrics.voice_score if metrics else 0.0,
            quality=quality,
            quality_reason=reason,
            notes=note,
        )
        self.logger.log(tx)

    def _flush_all(self, band: Band) -> None:
        for key, track in list(self.tracks.items()):
            if track.recording:
                self._finalize_track(key, track, band, force=True)
            else:
                self.tracks.pop(key, None)

    def _active_bands(self) -> list[Band]:
        """Bands in this radio's pool whose group is currently enabled in the UI."""
        pool = self.band_pool if self.band_pool is not None else self.cfg.bands
        return [b for b in pool if group_enabled(self.cfg, b.group)]

    def _hop_weight(self, band: Band) -> int:
        """Visit high-value voice groups more often (config scan.hop_weights)."""
        weights = getattr(self.cfg, "hop_weights", None) or {}
        try:
            w = int(weights.get(band.group, 1))
        except (TypeError, ValueError):
            w = 1
        return max(1, min(w, 20))

    def _next_enabled_band(self, current: Band | None) -> Band | None:
        active = self._active_bands()
        if not active:
            return None
        # Expand list by weight for simple weighted round-robin
        tickets: list[Band] = []
        for b in active:
            tickets.extend([b] * self._hop_weight(b))
        if not tickets:
            return None
        if current is None or current not in active:
            return tickets[0]
        # Advance to next ticket after last occurrence of current
        try:
            idx = len(tickets) - 1 - tickets[::-1].index(current)
        except ValueError:
            return tickets[0]
        return tickets[(idx + 1) % len(tickets)]

    def run(self) -> int:
        assert self.sdr is not None
        self._reload_squelch(force=True)
        band = self._next_enabled_band(None)
        if band is None:
            log.error(
                "[%s] No bands enabled for this radio — turn on groups in the viewer "
                "or check device.radios groups assignment",
                self.radio_label,
            )
            self.sdr.close()
            return 1

        # Background USB reader — DSP must never block sample ingest
        self.sdr.set_block_len(self._samples_per_look())
        self.sdr.start(self._samples_per_look())
        center = self._tune_band(band)
        log.info(
            "[%s] Monitoring band %s [%s]: %.3f–%.3f MHz (%s) · pool %d windows",
            self.radio_label,
            band.name,
            band.group,
            band.start_hz / 1e6,
            band.stop_hz / 1e6,
            band.modulation,
            len(self.band_pool),
        )
        n_labels = len(self.cfg.known_channels)
        n_gmrs = sum(1 for ch in self.cfg.known_channels if "GMRS" in ch.name or "FRS" in ch.name)
        log.info("[%s] Channel labels: %d total (%d GMRS/FRS)", self.radio_label, n_labels, n_gmrs)
        log.info(
            "[%s] IQ pipeline: async reader · survey FFT=%d×%d · dwell FFT=%d×%d · hop_idle=%d",
            self.radio_label,
            self.cfg.survey_fft_size,
            self.cfg.survey_averages,
            self.cfg.fft_size,
            self.cfg.averages,
            self.cfg.hop_after_idle_looks,
        )
        log.info(
            "[%s] Spur learning %.0fs/band then record. Quality filter=%s",
            self.radio_label,
            self.cfg.spur_learn_seconds,
            self.cfg.require_audio_quality,
        )
        if self._run_retention_loop:
            log.info("Logging to %s and %s", self.cfg.database, self.cfg.csv_path)
            log.info(
                "Audio retention: zip after %.0fh · delete after %.0fh (every %.0fs)",
                float(getattr(self.cfg, "audio_zip_after_hours", 12.0)),
                float(getattr(self.cfg, "audio_delete_after_hours", 72.0)),
                float(getattr(self.cfg, "retention_interval_seconds", 900.0)),
            )
            self._maybe_run_retention(force=True)

        looks_without_activity = 0
        hop_after = max(1, self.cfg.hop_after_idle_looks)
        max_band_dwell_s = float(getattr(self.cfg, "max_band_dwell_seconds", 25.0))
        looks_total = 0
        rate_t0 = time.monotonic()
        cycle_t0 = time.monotonic()
        band_entered = time.monotonic()
        bands_this_cycle = 0

        try:
            while not self._stop:
                self._reload_squelch()
                self._maybe_run_retention()
                hop_after = max(1, self.cfg.hop_after_idle_looks)
                # Keep reader block size aligned with survey/dwell mode
                self.sdr.set_block_len(self._samples_per_look())

                block = self.sdr.get_block(timeout=1.0)
                if block is None:
                    continue
                if block.dropped_before:
                    log.warning(
                        "[%s] IQ overrun: dropped %d block(s) (DSP behind USB)",
                        self.radio_label,
                        block.dropped_before,
                    )
                # Ignore blocks from previous center if any slipped through
                if abs(block.center_hz - center) > 1.0:
                    continue

                now = time.monotonic()
                peaks, iq, center = self._process_block(band, block)
                self._update_spur_learning(peaks)
                looks_total += 1

                if (time.monotonic() - rate_t0) >= 5.0:
                    elapsed = max(time.monotonic() - rate_t0, 1e-6)
                    ch_per_look = max(self.cfg.sample_rate_hz / max(self.cfg.channel_step_hz, 1), 1)
                    rate = (looks_total * ch_per_look) / elapsed
                    active_n = len(self._active_bands())
                    log.info(
                        "[%s] Scan rate ≈ %.0f ch-checks/s · %.1f looks/s · window %s · plan %d",
                        self.radio_label,
                        rate,
                        looks_total / elapsed,
                        band.name,
                        active_n,
                    )
                    rate_t0 = time.monotonic()
                    looks_total = 0

                if peaks:
                    looks_without_activity = 0
                    log.debug(
                        "[%s] Peaks: %s",
                        self.radio_label,
                        ", ".join(
                            f"{p.frequency_hz/1e6:.4f}MHz/{p.snr_db:.0f}dB" for p in peaks
                        ),
                    )
                else:
                    looks_without_activity += 1

                self._update_tracks(band, peaks, iq, center, now)

                # Hop when idle enough. Only *active recordings* block hops (not bare peak tracks).
                recording = any(t.recording for t in self.tracks.values())
                active = self._active_bands()
                if not active:
                    log.warning("[%s] All band groups disabled — waiting…", self.radio_label)
                    time.sleep(1.0)
                    continue

                dwell_s = time.monotonic() - band_entered
                force_time = dwell_s >= max_band_dwell_s and len(active) > 1
                group_off = band not in active
                idle_hop = (
                    not recording
                    and looks_without_activity >= hop_after
                    and len(active) > 1
                )
                # Continuous RF noise with no open recording should not pin us forever
                stuck_tracks = (
                    not recording
                    and self.tracks
                    and dwell_s >= max(8.0, max_band_dwell_s * 0.4)
                    and len(active) > 1
                )

                if force_time or group_off or idle_hop or stuck_tracks:
                    if recording and force_time:
                        log.info(
                            "[%s] Max dwell %.0fs on %s — finishing clips and hopping",
                            self.radio_label,
                            max_band_dwell_s,
                            band.name,
                        )
                    reason = (
                        "group_off" if group_off else
                        "max_dwell" if force_time else
                        "stuck_tracks" if stuck_tracks else
                        "idle"
                    )
                    self._flush_all(band)
                    self.tracks.clear()
                    prev = band
                    band = self._next_enabled_band(prev if prev in active else None)
                    if band is None:
                        continue
                    center = self._tune_band(band)
                    band_entered = time.monotonic()
                    looks_without_activity = 0
                    bands_this_cycle += 1
                    active = self._active_bands()
                    if active and band == active[0] and prev in active:
                        cycle_s = time.monotonic() - cycle_t0
                        log.info(
                            "[%s] Full enabled-band cycle in %.2fs (%d windows)",
                            self.radio_label,
                            cycle_s,
                            len(active),
                        )
                        cycle_t0 = time.monotonic()
                    log.info(
                        "[%s] Hop → %s [%s] (%.3f–%.3f MHz) %s  (%s)",
                        self.radio_label,
                        band.name,
                        band.group,
                        band.start_hz / 1e6,
                        band.stop_hz / 1e6,
                        band.modulation,
                        reason,
                    )
        except KeyboardInterrupt:
            log.info("[%s] Interrupted", self.radio_label)
        finally:
            self._flush_all(band)
            self.sdr.close()
            log.info("[%s] Shutdown complete", self.radio_label)
        return 0


def _run_multi(cfg: Config) -> int:
    """Start one worker thread per configured radio (shared log + live hub)."""
    radios = resolve_devices(list(cfg.radios))
    assignment = partition_bands(cfg.bands, radios)
    stop_event = threading.Event()
    shared_log = TransmissionLog(cfg.database, cfg.csv_path, cfg.output_dir)
    live_hub = LiveHub(cfg.output_dir / "live_state.json")

    workers: list[tuple[ScannerApp, threading.Thread]] = []
    for i, radio in enumerate(radios):
        pool = assignment.get(radio.label) or []
        if not pool:
            log.warning("[%s] no band windows assigned — skipping", radio.label)
            continue
        app = ScannerApp(
            cfg,
            radio=radio,
            band_pool=pool,
            logger=shared_log,
            live_hub=live_hub,
            stop_event=stop_event,
            run_retention_loop=(i == 0),
            open_sdr=True,
        )
        th = threading.Thread(
            target=app.run,
            name=f"radio-{radio.label}",
            daemon=True,
        )
        workers.append((app, th))

    if not workers:
        log.error("No radio workers started")
        return 1

    def _stop(*_a) -> None:
        log.info("Stop requested — shutting down %d radio(s)…", len(workers))
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("Multi-dongle: starting %d radio worker(s)", len(workers))
    for _app, th in workers:
        th.start()

    # Keep main thread alive; first worker owns retention
    while not stop_event.is_set():
        alive = any(th.is_alive() for _a, th in workers)
        if not alive:
            break
        time.sleep(0.4)

    stop_event.set()
    for _app, th in workers:
        th.join(timeout=5.0)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan RF bands with one or more RTL-SDRs, log transmissions, record audio."
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List connected RTL-SDR serials/indexes and exit",
    )
    args = parser.parse_args(argv)

    if args.list_devices:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        try:
            devices = list_rtlsdr_devices()
        except SystemExit as e:
            print(e, file=sys.stderr)
            return 2
        if not devices:
            print("No RTL-SDR devices found.")
            return 1
        print(f"Found {len(devices)} RTL-SDR device(s):")
        for d in devices:
            print(f"  {d.display()}")
        print("\nPut serials under device.radios in config.yaml, e.g.:")
        print("  device:")
        print("    radios:")
        for i, d in enumerate(devices):
            label = "voice" if i == 0 else ("atc" if i == 1 else f"r{i}")
            ser = d.serial or "null"
            print(f"      - label: {label}")
            print(f"        serial: \"{ser}\"" if d.serial else f"        device_index: {d.index}")
        return 0

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        return 2

    cfg = Config.from_yaml(cfg_path)
    level = logging.DEBUG if args.verbose else getattr(logging, cfg.log_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Prevent two scanner processes fighting dongles
    acquire_lock(Path(cfg.output_dir).parent / "logs" / "scanner.lock")

    n_radios = len(cfg.radios) if cfg.radios else 1
    if n_radios > 1:
        return _run_multi(cfg)

    # Single-dongle path (same process, one worker)
    radio = cfg.radios[0] if cfg.radios else RadioConfig(
        label="R0", serial=cfg.serial, gain=cfg.gain, ppm_error=cfg.ppm_error
    )
    try:
        radio = resolve_devices([radio])[0]
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    app = ScannerApp(cfg, radio=radio, band_pool=list(cfg.bands))
    signal.signal(signal.SIGINT, app.request_stop)
    signal.signal(signal.SIGTERM, app.request_stop)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
