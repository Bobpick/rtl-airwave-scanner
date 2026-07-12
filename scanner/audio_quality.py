from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class AudioMetrics:
    rms: float
    peak: float
    dynamic_range_db: float
    activity_ratio: float
    spectral_flux: float
    speech_band_ratio: float
    voice_score: float
    is_likely_signal: bool
    reason: str


def analyze_audio(
    audio: np.ndarray,
    sample_rate: int,
    min_rms: float = 0.015,
    min_dynamic_range_db: float = 7.0,
    min_activity_ratio: float = 0.08,
    max_activity_ratio: float = 0.98,
    min_voice_score: float = 0.40,
    min_silence_ratio: float = 0.05,
    min_speech_band_ratio: float = 0.35,
    loose: bool = False,
) -> AudioMetrics:
    """Score demodulated audio.

    When *loose* (open squelch / AM ATC), only pure silence is rejected so
    weak or clipped aviation audio can still be saved.
    """
    if audio is None or len(audio) < sample_rate * 0.15:
        return AudioMetrics(0, 0, 0, 0, 0, 0, 0, False, "too_short")

    x = np.asarray(audio, dtype=np.float64)
    x = x - np.mean(x)
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x * x)))

    # Completely empty / digital zero
    if peak < 1e-6 or rms < 1e-6:
        return AudioMetrics(0, 0, 0, 0, 0, 0, 0, False, "silence")

    frame = max(int(sample_rate * 0.02), 1)
    n_frames = len(x) // frame
    if n_frames < 4:
        # Short clip: accept if not silent when loose
        ok = rms >= min_rms * (0.25 if loose else 0.5)
        return AudioMetrics(
            rms, peak, 0, 1.0 if ok else 0.0, 0, 0, 0.5 if ok else 0.0, ok,
            "ok" if ok else "too_few_frames",
        )

    frames = x[: n_frames * frame].reshape(n_frames, frame)
    frame_rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-20)
    med = float(np.median(frame_rms))
    floor = float(np.percentile(frame_rms, 20))
    crest = float(np.percentile(frame_rms, 90))
    # Softer activity threshold so weak AM speech still counts
    thr = max(med * 1.25, floor * 1.6, min_rms * 0.25)
    activity = frame_rms > thr
    activity_ratio = float(np.mean(activity))
    silence_ratio = float(np.mean(~activity))

    p95 = float(np.percentile(frame_rms, 95))
    p10 = float(np.percentile(frame_rms, 10) + 1e-12)
    dynamic_range_db = float(20.0 * np.log10((p95 + 1e-9) / p10))
    env_cv = float(np.std(frame_rms) / (np.mean(frame_rms) + 1e-12))

    n_fft = 512
    hop = n_fft // 2
    speech_e = 0.0
    total_e = 0.0
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    speech_bins = (freqs >= 250) & (freqs <= 3500)
    for i in range(0, len(x) - n_fft, hop):
        window = x[i : i + n_fft] * np.hanning(n_fft)
        mag2 = np.abs(np.fft.rfft(window)) ** 2
        total_e += float(np.sum(mag2))
        speech_e += float(np.sum(mag2[speech_bins]))
    speech_band_ratio = speech_e / (total_e + 1e-20)

    specs = []
    for i in range(0, len(x) - n_fft, hop * 2):
        window = x[i : i + n_fft] * np.hanning(n_fft)
        mag = np.abs(np.fft.rfft(window))
        specs.append(mag / (np.sum(mag) + 1e-12))
    spectral_flux = 0.0
    if len(specs) >= 2:
        spectral_flux = float(np.mean(np.abs(np.diff(np.stack(specs), axis=0))))

    # Weighted score 0–1 (uses full slider thresholds so UI matches accept logic)
    score = 0.0
    if rms >= min_rms:
        score += 0.2
    elif rms >= min_rms * 0.5:
        score += 0.1
    if dynamic_range_db >= min_dynamic_range_db:
        score += 0.2
    elif dynamic_range_db >= min_dynamic_range_db * 0.75:
        score += 0.1
    if activity_ratio >= min_activity_ratio:
        score += 0.25
    elif activity_ratio >= min_activity_ratio * 0.75:
        score += 0.1
    if env_cv >= 0.12:
        score += 0.15
    elif env_cv >= 0.08:
        score += 0.08
    if speech_band_ratio >= min_speech_band_ratio:
        score += 0.15
    elif speech_band_ratio >= min_speech_band_ratio * 0.7:
        score += 0.08
    if spectral_flux > 0.008:
        score += 0.05
    score = float(min(score, 1.0))

    reasons = []
    if rms < min_rms:
        reasons.append("low_rms")
    if dynamic_range_db < min_dynamic_range_db:
        reasons.append("flat_level")
    if activity_ratio < min_activity_ratio:
        reasons.append("low_activity")
    if speech_band_ratio < min_speech_band_ratio:
        reasons.append("not_speechy")
    if score < min_voice_score:
        reasons.append("low_voice_score")

    if loose:
        # AM only (caller must set loose=True solely for AM).
        # Still honor slider floors for activity / DR / voice — relaxed slightly.
        min_loose_rms = max(min_rms * 0.35, 0.0035)
        min_loose_peak = max(0.015, min_rms * 0.7)
        act_need = max(0.03, min_activity_ratio * 0.65)
        dr_need = max(3.0, min_dynamic_range_db * 0.65)
        voice_need = max(0.15, min_voice_score * 0.75)
        has_bursts = activity_ratio >= act_need
        has_dr = dynamic_range_db >= dr_need
        speechy_enough = speech_band_ratio >= min_speech_band_ratio * 0.40
        ok = (
            rms >= min_loose_rms
            and peak >= min_loose_peak
            and has_bursts
            and has_dr
            and score >= voice_need
            and speechy_enough
        )
        if not ok:
            if rms < min_loose_rms or peak < min_loose_peak:
                reasons.append("am_weak")
            if not has_bursts:
                reasons.append("am_low_activity")
            if not has_dr:
                reasons.append("am_flat_level")
            if score < voice_need:
                reasons.append("am_low_voice")
            if not speechy_enough:
                reasons.append("am_hissy_band")
            reason = ",".join(reasons) if reasons else "am_reject"
        else:
            reason = "ok_loose"
    else:
        # Tight (NFM / default): EVERY slider is a hard gate — matches UI expectations
        ok = (
            score >= min_voice_score
            and rms >= min_rms * 0.5
            and activity_ratio >= min_activity_ratio
            and dynamic_range_db >= min_dynamic_range_db
        )
        # Steady keyed-mic / open carrier: almost no envelope motion
        if env_cv < 0.06 and activity_ratio < max(0.08, min_activity_ratio):
            ok = False
            if "steady_envelope" not in reasons:
                reasons.append("steady_envelope")
        reason = "ok" if ok else (",".join(reasons) or "rejected")

    return AudioMetrics(
        rms=rms,
        peak=peak,
        dynamic_range_db=dynamic_range_db,
        activity_ratio=activity_ratio,
        spectral_flux=spectral_flux,
        speech_band_ratio=speech_band_ratio,
        voice_score=score,
        is_likely_signal=ok,
        reason=reason,
    )
