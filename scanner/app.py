from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from scanner.audio_quality import analyze_audio
from scanner.config import Band, Config
from scanner.demod import demodulate
from scanner.recorder import Transmission, TransmissionLog, utcnow
from scanner.sdr import RtlSdrSource
from scanner.spectrum import find_peaks, power_spectrum_db
from scanner.live_state import write_live_state
from scanner.squelch import apply_to_config, group_enabled, load_squelch

log = logging.getLogger(__name__)


@dataclass
class ActiveTrack:
    frequency_hz: float
    first_seen: float
    last_seen: float
    peak_snr_db: float
    snr_samples: list[float] = field(default_factory=list)
    audio_chunks: list[np.ndarray] = field(default_factory=list)
    recording: bool = False
    start_time: object | None = None


class ScannerApp:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._stop = False
        self.logger = TransmissionLog(cfg.database, cfg.csv_path, cfg.output_dir)
        self.sdr = RtlSdrSource(
            sample_rate_hz=cfg.sample_rate_hz,
            gain=cfg.gain,
            ppm_error=cfg.ppm_error,
            serial=cfg.serial,
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

    def request_stop(self, *_args) -> None:
        log.info("Stop requested…")
        self._stop = True

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
            ("ham", self.cfg.enable_ham),
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
        sr = self.cfg.sample_rate_hz
        usable = sr * 0.9
        if band.width_hz > usable:
            log.warning(
                "Band %s width %.1f kHz exceeds ~%.1f kHz usable span; "
                "only center portion will be monitored",
                band.name,
                band.width_hz / 1e3,
                usable / 1e3,
            )
        center = band.center_hz
        self.sdr.set_center(center)
        # Minimal settle buffer (keeps hop rate high)
        self.sdr.read_samples(min(self.cfg.survey_fft_size, 4096))
        # Reset spur learning per band hop
        self._look_count = 0
        self._hit_counts.clear()
        self._auto_ignored.clear()
        self._learn_started = time.monotonic()
        self._learning_done = False
        return center

    def _look(self, band: Band, center: float):
        fft_size, _averages = self._fft_params()
        n = self._samples_per_look()
        iq = self.sdr.read_samples(n)
        freqs, power = power_spectrum_db(
            iq, self.sdr.sample_rate, center, fft_size
        )
        guard = self.sdr.sample_rate * self.cfg.edge_guard_fraction
        lo = max(band.start_hz, center - self.sdr.sample_rate / 2 + guard)
        hi = min(band.stop_hz, center + self.sdr.sample_rate / 2 - guard)
        # Slightly lower SNR bar in survey so weak hits trigger a dwell
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

        # Feed the dashboard spectrum / waterfall
        try:
            self._mode = "DWELL" if any(t.recording for t in self.tracks.values()) else "SURVEY"
            try:
                g = self.sdr._sdr.gain
                gain = float(g) if not isinstance(g, str) else 0.0
            except Exception:
                gain = float(self.cfg.gain or 0)
            plan = [
                {
                    "name": b.name,
                    "group": b.group,
                    "start_mhz": b.start_hz / 1e6,
                    "stop_mhz": b.stop_hz / 1e6,
                    "active": b.name == band.name,
                }
                for b in self._active_bands()
            ]
            write_live_state(
                site_name=self._site_name,
                mode=self._mode,
                gain=gain,
                band_name=band.name,
                group=band.group,
                center_hz=center,
                sample_rate=self.sdr.sample_rate,
                freqs_hz=freqs,
                power_db=power,
                peaks=[
                    {"mhz": p.frequency_hz / 1e6, "snr_db": p.snr_db, "group": band.group}
                    for p in peaks[:12]
                ],
                plan=plan,
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

    def _update_tracks(self, band: Band, peaks, iq: np.ndarray, center: float, now: float) -> None:
        # During spur learning, still observe hits but do not start recordings
        learning = not self._learning_done
        seen_keys = set()
        ch_bw = self._channel_bw(band)

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
                )
                self.tracks[key] = track
                log.debug("New energy @ %.4f MHz snr=%.1f", channel_hz / 1e6, peak.snr_db)
            else:
                track.last_seen = now
                track.frequency_hz = channel_hz
                track.peak_snr_db = max(track.peak_snr_db, peak.snr_db)
                track.snr_samples.append(peak.snr_db)

            if learning:
                continue

            active_for = now - track.first_seen
            if not track.recording and active_for >= self.cfg.min_active_seconds:
                track.recording = True
                track.start_time = utcnow()
                ch = self.cfg.match_channel(track.frequency_hz)
                label = f"  [{ch.name}]" if ch else ""
                log.info(
                    "START  %.4f MHz  band=%s  snr=%.1f dB%s",
                    track.frequency_hz / 1e6,
                    band.name,
                    track.peak_snr_db,
                    label,
                )

            if track.recording:
                # AM: no de-emphasis. NFM/FM: use configured tau (US 75 µs).
                tau = None if band.modulation == "am" else self.cfg.deemphasis_tau
                audio = demodulate(
                    iq,
                    self.sdr.sample_rate,
                    center,
                    track.frequency_hz,
                    ch_bw,
                    band.modulation,
                    self.cfg.audio_sample_rate_hz,
                    tau,
                )
                track.audio_chunks.append(audio)

                if track.start_time is not None:
                    elapsed = (utcnow() - track.start_time).total_seconds()
                    if elapsed >= self.cfg.max_recording_seconds:
                        self._finalize_track(key, track, band, force=True)

        hang = self.cfg.hang_time_seconds
        for key, track in list(self.tracks.items()):
            if key in seen_keys:
                continue
            if learning:
                # drop non-persistent during learn without saving
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
        if duration < self.cfg.min_active_seconds and not force:
            log.debug("Drop short blip @ %.4f MHz (%.2fs)", track.frequency_hz / 1e6, duration)
            return

        mean_snr = float(np.mean(track.snr_samples)) if track.snr_samples else track.peak_snr_db
        audio = None
        metrics = None
        if track.audio_chunks:
            audio = np.concatenate(track.audio_chunks)
            # Open squelch when voice slider is low; always looser for AM ATC
            loose = self.cfg.min_voice_score <= 0.30 or band.modulation == "am"
            metrics = analyze_audio(
                audio,
                self.cfg.audio_sample_rate_hz,
                min_rms=self.cfg.min_audio_rms,
                min_dynamic_range_db=self.cfg.min_dynamic_range_db,
                min_activity_ratio=self.cfg.min_activity_ratio,
                min_voice_score=self.cfg.min_voice_score,
                min_speech_band_ratio=self.cfg.min_speech_band_ratio,
                loose=loose,
            )
            # Only apply steady-SNR reject when squelch is tight (not open)
            if (
                not loose
                and track.snr_samples
                and len(track.snr_samples) >= 6
                and metrics is not None
            ):
                snr_std = float(np.std(track.snr_samples))
                if snr_std < 0.5 and metrics.voice_score < 0.5:
                    metrics = type(metrics)(
                        rms=metrics.rms,
                        peak=metrics.peak,
                        dynamic_range_db=metrics.dynamic_range_db,
                        activity_ratio=metrics.activity_ratio,
                        spectral_flux=metrics.spectral_flux,
                        speech_band_ratio=metrics.speech_band_ratio,
                        voice_score=metrics.voice_score,
                        is_likely_signal=False,
                        reason=(metrics.reason + ",steady_snr").strip(","),
                    )

        quality = "accepted"
        reason = "ok"
        audio_path = None
        min_samples = int(self.cfg.audio_sample_rate_hz * 0.25)

        if metrics is None or audio is None:
            quality = "rejected"
            reason = "no_audio"
        elif len(audio) < min_samples:
            quality = "rejected"
            reason = "audio_too_short"
        elif self.cfg.require_audio_quality and not metrics.is_likely_signal:
            quality = "rejected"
            reason = metrics.reason
        else:
            audio_path = self.logger.save_audio(
                audio,
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
                and audio is not None
                and len(audio) > min_samples
            ):
                audio_path = self.logger.save_audio(
                    audio,
                    self.cfg.audio_sample_rate_hz,
                    track.frequency_hz,
                    track.start_time,
                    suffix="_rejected",
                )
        else:
            log.info(
                "ACCEPT %.4f MHz  voice=%.2f act=%.0f%% snr=%.1f",
                track.frequency_hz / 1e6,
                metrics.voice_score if metrics else 0.0,
                (metrics.activity_ratio * 100) if metrics else 0.0,
                track.peak_snr_db,
            )

        ch = self.cfg.match_channel(track.frequency_hz)
        if ch:
            note = f"{ch.name} — {ch.notes}" if ch.notes else ch.name
        else:
            note = ""

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
        """Bands whose group is currently enabled in the UI."""
        out = [b for b in self.cfg.bands if group_enabled(self.cfg, b.group)]
        return out

    def _next_enabled_band(self, current: Band | None) -> Band | None:
        active = self._active_bands()
        if not active:
            return None
        if current is None or current not in active:
            return active[0]
        i = active.index(current)
        return active[(i + 1) % len(active)]

    def run(self) -> int:
        self._reload_squelch(force=True)
        band = self._next_enabled_band(None)
        if band is None:
            log.error("No bands enabled — turn on ATC/ham/GMRS/… in the viewer")
            self.sdr.close()
            return 1
        center = self._tune_band(band)
        log.info(
            "Monitoring band %s [%s]: %.3f–%.3f MHz (%s)",
            band.name,
            band.group,
            band.start_hz / 1e6,
            band.stop_hz / 1e6,
            band.modulation,
        )
        n_labels = len(self.cfg.known_channels)
        n_gmrs = sum(1 for ch in self.cfg.known_channels if "GMRS" in ch.name or "FRS" in ch.name)
        log.info("Channel labels: %d total (%d GMRS/FRS)", n_labels, n_gmrs)
        log.info(
            "Fast survey: FFT=%d×%d, hop after %d idle looks · dwell FFT=%d×%d when recording",
            self.cfg.survey_fft_size,
            self.cfg.survey_averages,
            self.cfg.hop_after_idle_looks,
            self.cfg.fft_size,
            self.cfg.averages,
        )
        log.info(
            "Spur learning %.0fs/band then record. Quality filter=%s",
            self.cfg.spur_learn_seconds,
            self.cfg.require_audio_quality,
        )
        log.info("Logging to %s and %s", self.cfg.database, self.cfg.csv_path)

        looks_without_activity = 0
        hop_after = max(1, self.cfg.hop_after_idle_looks)
        # Don't pin forever on one window (noise/spurs used to block hops while tracks existed)
        max_band_dwell_s = float(getattr(self.cfg, "max_band_dwell_seconds", 25.0))
        looks_total = 0
        rate_t0 = time.monotonic()
        cycle_t0 = time.monotonic()
        band_entered = time.monotonic()
        bands_this_cycle = 0

        try:
            while not self._stop:
                self._reload_squelch()
                hop_after = max(1, self.cfg.hop_after_idle_looks)
                now = time.monotonic()
                peaks, iq, center = self._look(band, center)
                self._update_spur_learning(peaks)
                looks_total += 1

                # Channel-equivalent rate: one wideband look covers ~sr/step channels
                if looks_total == 1 or (time.monotonic() - rate_t0) >= 5.0:
                    elapsed = max(time.monotonic() - rate_t0, 1e-6)
                    ch_per_look = max(self.cfg.sample_rate_hz / max(self.cfg.channel_step_hz, 1), 1)
                    rate = (looks_total * ch_per_look) / elapsed
                    active_n = len(self._active_bands())
                    log.info(
                        "Scan rate ≈ %.0f ch-checks/s · window %s · plan %d bands",
                        rate,
                        band.name,
                        active_n,
                    )
                    rate_t0 = time.monotonic()
                    looks_total = 0

                if peaks:
                    looks_without_activity = 0
                    log.debug(
                        "Peaks: %s",
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
                    log.warning("All band groups disabled — waiting…")
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
                            "Max dwell %.0fs on %s — finishing clips and hopping",
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
                            "Full enabled-band cycle in %.2fs (%d windows)",
                            cycle_s,
                            len(active),
                        )
                        cycle_t0 = time.monotonic()
                    log.info(
                        "Hop → %s [%s] (%.3f–%.3f MHz) %s  (%s)",
                        band.name,
                        band.group,
                        band.start_hz / 1e6,
                        band.stop_hz / 1e6,
                        band.modulation,
                        reason,
                    )
        except KeyboardInterrupt:
            log.info("Interrupted")
        finally:
            self._flush_all(band)
            self.sdr.close()
            log.info("Shutdown complete")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan RF bands with an RTL-SDR, log transmissions, record audio."
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
    args = parser.parse_args(argv)

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

    app = ScannerApp(cfg)
    signal.signal(signal.SIGINT, app.request_stop)
    signal.signal(signal.SIGTERM, app.request_stop)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
