"""Audio retention: zip WAVs after N hours, delete archives after M hours.

Default policy (SIGINT disk hygiene):
  - WAVs older than 12 hours → compress into recordings/archive/*.wav.zip
  - Archives (and leftover WAVs) older than 72 hours → delete
  - SQLite audio_file paths are updated so the viewer can still play from zip
  - transmissions.db / .csv / live_state.json are never touched as media
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Filename stamp from save_audio: 20260711T154910Z_450.1375MHz.wav
_STAMP_RE = re.compile(r"^(\d{8}T\d{6}Z)_")

SKIP_NAMES = {
    "live_state.json",
    "transmissions.db",
    "transmissions.csv",
    "transmissions.db-journal",
    "transmissions.db-wal",
    "transmissions.db-shm",
}


@dataclass
class RetentionResult:
    zipped: int = 0
    deleted: int = 0
    errors: int = 0
    bytes_freed: int = 0


def file_age_seconds(path: Path, now: float | None = None) -> float:
    """Age from filename UTC stamp when present, else mtime.

    Handles plain WAVs and archive names like ``stamp_freq.wav.zip``.
    """
    now = time.time() if now is None else now
    name = path.name
    if name.endswith(".wav.zip"):
        name = name[: -len(".zip")]  # …MHz.wav
    m = _STAMP_RE.match(name)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return max(0.0, now - dt.timestamp())
        except ValueError:
            pass
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return 0.0


def archive_path_for_wav(audio_dir: Path, wav: Path) -> Path:
    """recordings/foo.wav → recordings/archive/foo.wav.zip"""
    return audio_dir / "archive" / f"{wav.name}.zip"


def _update_db_audio_path(db_path: Path | None, old: str, new: str) -> None:
    if not db_path or not db_path.is_file():
        return
    variants = {old, str(Path(old)), Path(old).name}
    # Also absolute / relative variants of same basename
    try:
        variants.add(str(Path(old).resolve()))
    except OSError:
        pass
    try:
        with sqlite3.connect(db_path) as conn:
            for v in variants:
                conn.execute(
                    "UPDATE transmissions SET audio_file = ? WHERE audio_file = ?",
                    (new, v),
                )
                # Match trailing path ending with same basename
                conn.execute(
                    "UPDATE transmissions SET audio_file = ? WHERE audio_file LIKE ?",
                    (new, f"%/{Path(old).name}"),
                )
            conn.commit()
    except sqlite3.Error as e:
        log.warning("DB audio_file update failed (%s → %s): %s", old, new, e)


def _clear_db_audio_path(db_path: Path | None, path: Path) -> None:
    if not db_path or not db_path.is_file():
        return
    name = path.name
    # strip .zip if present for matching
    bare = name[:-4] if name.endswith(".zip") else name
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE transmissions SET audio_file = NULL WHERE audio_file LIKE ?",
                (f"%{name}%",),
            )
            if bare != name:
                conn.execute(
                    "UPDATE transmissions SET audio_file = NULL WHERE audio_file LIKE ?",
                    (f"%{bare}%",),
                )
            conn.commit()
    except sqlite3.Error as e:
        log.warning("DB audio_file clear failed for %s: %s", path, e)


def zip_wav(wav: Path, zip_path: Path) -> int:
    """Compress one WAV into zip_path (single member = basename). Returns bytes written."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_suffix(zip_path.suffix + ".partial")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(wav, arcname=wav.name)
        tmp.replace(zip_path)
        return zip_path.stat().st_size
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def run_retention(
    audio_dir: Path,
    *,
    zip_after_hours: float = 12.0,
    delete_after_hours: float = 72.0,
    db_path: Path | None = None,
    dry_run: bool = False,
) -> RetentionResult:
    """
    Apply retention policy under *audio_dir*.

    - *.wav older than zip_after_hours → archive/<name>.wav.zip, remove wav
    - archive/*.zip older than delete_after_hours → delete
    - leftover *.wav older than delete_after_hours → delete
    """
    result = RetentionResult()
    audio_dir = Path(audio_dir)
    if not audio_dir.is_dir():
        return result

    now = time.time()
    zip_after_s = max(0.0, float(zip_after_hours)) * 3600.0
    delete_after_s = max(0.0, float(delete_after_hours)) * 3600.0
    if delete_after_s and zip_after_s and delete_after_s < zip_after_s:
        log.warning(
            "delete_after_hours (%.1f) < zip_after_hours (%.1f); using zip age as delete floor",
            delete_after_hours,
            zip_after_hours,
        )
        delete_after_s = zip_after_s

    archive_dir = audio_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # --- Zip aged WAVs ---
    for wav in sorted(audio_dir.glob("*.wav")):
        if wav.name in SKIP_NAMES:
            continue
        age = file_age_seconds(wav, now)
        if age < zip_after_s:
            continue
        zpath = archive_path_for_wav(audio_dir, wav)
        try:
            if dry_run:
                log.info("DRY-RUN zip %s (age %.1fh) → %s", wav.name, age / 3600.0, zpath.name)
                result.zipped += 1
                continue
            if zpath.is_file():
                # Already archived; drop loose WAV
                size = wav.stat().st_size
                wav.unlink(missing_ok=True)
                result.bytes_freed += size
                result.zipped += 1
                _update_db_audio_path(db_path, str(wav), str(zpath))
                continue
            zip_wav(wav, zpath)
            size = wav.stat().st_size
            wav.unlink(missing_ok=True)
            result.bytes_freed += size
            result.zipped += 1
            _update_db_audio_path(db_path, str(wav), str(zpath))
            # Prefer relative-ish path stored as used historically
            _update_db_audio_path(db_path, str(wav.resolve()), str(zpath))
            log.info("Archived %s → %s (age %.1fh)", wav.name, zpath.relative_to(audio_dir), age / 3600.0)
        except Exception as e:
            result.errors += 1
            log.warning("Failed to archive %s: %s", wav, e)

    # --- Delete aged archives ---
    if archive_dir.is_dir():
        for zpath in sorted(archive_dir.glob("*.zip")):
            age = file_age_seconds(zpath, now)
            if age < delete_after_s:
                continue
            try:
                if dry_run:
                    log.info("DRY-RUN delete %s (age %.1fh)", zpath.name, age / 3600.0)
                    result.deleted += 1
                    continue
                size = zpath.stat().st_size
                zpath.unlink(missing_ok=True)
                result.bytes_freed += size
                result.deleted += 1
                _clear_db_audio_path(db_path, zpath)
                log.info("Deleted archive %s (age %.1fh)", zpath.name, age / 3600.0)
            except Exception as e:
                result.errors += 1
                log.warning("Failed to delete %s: %s", zpath, e)

    # --- Delete leftover WAVs past delete horizon (zip failed / disabled) ---
    for wav in sorted(audio_dir.glob("*.wav")):
        age = file_age_seconds(wav, now)
        if age < delete_after_s:
            continue
        try:
            if dry_run:
                log.info("DRY-RUN delete aged wav %s (age %.1fh)", wav.name, age / 3600.0)
                result.deleted += 1
                continue
            size = wav.stat().st_size
            wav.unlink(missing_ok=True)
            result.bytes_freed += size
            result.deleted += 1
            _clear_db_audio_path(db_path, wav)
            log.info("Deleted aged wav %s (age %.1fh)", wav.name, age / 3600.0)
        except Exception as e:
            result.errors += 1
            log.warning("Failed to delete %s: %s", wav, e)

    if result.zipped or result.deleted:
        log.info(
            "Retention: zipped=%d deleted=%d errors=%d freed≈%.1f MiB",
            result.zipped,
            result.deleted,
            result.errors,
            result.bytes_freed / (1024 * 1024),
        )
    return result


def open_audio_bytes(path: Path) -> tuple[bytes, str] | None:
    """
    Load WAV bytes from a plain .wav or a .wav.zip archive.
    Returns (bytes, download_name) or None.
    """
    path = Path(path)
    if not path.is_file():
        return None
    name = path.name
    if name.endswith(".zip"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                # Prefer first .wav member
                members = [n for n in zf.namelist() if n.lower().endswith(".wav")]
                if not members:
                    members = list(zf.namelist())
                if not members:
                    return None
                member = members[0]
                data = zf.read(member)
                dl = Path(member).name
                return data, dl
        except zipfile.BadZipFile:
            return None
    if name.lower().endswith(".wav"):
        return path.read_bytes(), name
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    from scanner.config import Config

    parser = argparse.ArgumentParser(description="Zip/delete aged scanner audio files")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--zip-after-hours", type=float, default=None)
    parser.add_argument("--delete-after-hours", type=float, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config.from_yaml(Path(args.config))
    zip_h = args.zip_after_hours if args.zip_after_hours is not None else cfg.audio_zip_after_hours
    del_h = (
        args.delete_after_hours
        if args.delete_after_hours is not None
        else cfg.audio_delete_after_hours
    )
    r = run_retention(
        cfg.output_dir,
        zip_after_hours=zip_h,
        delete_after_hours=del_h,
        db_path=cfg.database,
        dry_run=args.dry_run,
    )
    print(f"zipped={r.zipped} deleted={r.deleted} errors={r.errors} freed_bytes={r.bytes_freed}")
    return 0 if r.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
