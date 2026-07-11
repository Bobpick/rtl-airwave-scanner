from __future__ import annotations

import csv
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

COLUMNS = [
    "id",
    "start_utc",
    "end_utc",
    "frequency_hz",
    "frequency_mhz",
    "band_name",
    "modulation",
    "peak_snr_db",
    "mean_snr_db",
    "duration_seconds",
    "audio_file",
    "audio_rms",
    "audio_peak",
    "dynamic_range_db",
    "activity_ratio",
    "voice_score",
    "quality",
    "quality_reason",
    "notes",
]


@dataclass
class Transmission:
    start_utc: datetime
    end_utc: datetime
    frequency_hz: float
    band_name: str
    modulation: str
    peak_snr_db: float
    mean_snr_db: float
    audio_path: Path | None
    duration_seconds: float
    audio_rms: float = 0.0
    audio_peak: float = 0.0
    dynamic_range_db: float = 0.0
    activity_ratio: float = 0.0
    voice_score: float = 0.0
    quality: str = "accepted"  # accepted | rejected
    quality_reason: str = "ok"
    notes: str = ""


class TransmissionLog:
    def __init__(self, db_path: Path, csv_path: Path, audio_dir: Path) -> None:
        self.db_path = db_path
        self.csv_path = csv_path
        self.audio_dir = audio_dir
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # multi-dongle workers share one log
        self._init_db()
        self._init_csv()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transmissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_utc TEXT NOT NULL,
                    end_utc TEXT NOT NULL,
                    frequency_hz REAL NOT NULL,
                    frequency_mhz REAL NOT NULL,
                    band_name TEXT,
                    modulation TEXT,
                    peak_snr_db REAL,
                    mean_snr_db REAL,
                    duration_seconds REAL,
                    audio_file TEXT,
                    audio_rms REAL,
                    audio_peak REAL,
                    dynamic_range_db REAL,
                    activity_ratio REAL,
                    voice_score REAL,
                    quality TEXT,
                    quality_reason TEXT,
                    notes TEXT
                )
                """
            )
            # Migrate older DBs
            existing = {
                row[1] for row in conn.execute("PRAGMA table_info(transmissions)").fetchall()
            }
            for col, typ in [
                ("mean_snr_db", "REAL"),
                ("audio_rms", "REAL"),
                ("audio_peak", "REAL"),
                ("dynamic_range_db", "REAL"),
                ("activity_ratio", "REAL"),
                ("voice_score", "REAL"),
                ("quality", "TEXT"),
                ("quality_reason", "TEXT"),
                ("notes", "TEXT"),
            ]:
                if col not in existing:
                    conn.execute(f"ALTER TABLE transmissions ADD COLUMN {col} {typ}")
            conn.commit()

    def _init_csv(self) -> None:
        if not self.csv_path.exists():
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([c for c in COLUMNS if c != "id"])

    def save_audio(
        self,
        audio: np.ndarray,
        sample_rate: int,
        frequency_hz: float,
        start: datetime,
        suffix: str = "",
    ) -> Path:
        stamp = start.strftime("%Y%m%dT%H%M%SZ")
        mhz = frequency_hz / 1e6
        name = f"{stamp}_{mhz:.4f}MHz{suffix}.wav"
        path = self.audio_dir / name
        with self._lock:
            sf.write(str(path), audio, sample_rate, subtype="PCM_16")
        return path

    def log(self, tx: Transmission) -> int:
        audio_str = str(tx.audio_path) if tx.audio_path else ""
        values = (
            tx.start_utc.isoformat(),
            tx.end_utc.isoformat(),
            tx.frequency_hz,
            tx.frequency_hz / 1e6,
            tx.band_name,
            tx.modulation,
            tx.peak_snr_db,
            tx.mean_snr_db,
            tx.duration_seconds,
            audio_str,
            tx.audio_rms,
            tx.audio_peak,
            tx.dynamic_range_db,
            tx.activity_ratio,
            tx.voice_score,
            tx.quality,
            tx.quality_reason,
            tx.notes,
        )
        headers = [c for c in COLUMNS if c != "id"]
        with self._lock:
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(
                    [
                        tx.start_utc.isoformat(),
                        tx.end_utc.isoformat(),
                        f"{tx.frequency_hz:.1f}",
                        f"{tx.frequency_hz / 1e6:.6f}",
                        tx.band_name,
                        tx.modulation,
                        f"{tx.peak_snr_db:.2f}",
                        f"{tx.mean_snr_db:.2f}",
                        f"{tx.duration_seconds:.3f}",
                        audio_str,
                        f"{tx.audio_rms:.5f}",
                        f"{tx.audio_peak:.5f}",
                        f"{tx.dynamic_range_db:.2f}",
                        f"{tx.activity_ratio:.3f}",
                        f"{tx.voice_score:.3f}",
                        tx.quality,
                        tx.quality_reason,
                        tx.notes,
                    ]
                )

            with self._connect() as conn:
                cur = conn.execute(
                    f"""
                    INSERT INTO transmissions (
                        {", ".join(headers)}
                    ) VALUES ({", ".join("?" for _ in headers)})
                    """,
                    values,
                )
                conn.commit()
                row_id = int(cur.lastrowid)

        tag = "SAVED" if tx.quality == "accepted" else "REJECT"
        log.info(
            "%s %.4f MHz  %s–%s  %.1fs  snr=%.1f  voice=%.2f  %s  %s",
            tag,
            tx.frequency_hz / 1e6,
            tx.start_utc.strftime("%H:%M:%S"),
            tx.end_utc.strftime("%H:%M:%S"),
            tx.duration_seconds,
            tx.peak_snr_db,
            tx.voice_score,
            tx.quality_reason,
            tx.audio_path.name if tx.audio_path else "(no audio)",
        )
        return row_id

    def list_transmissions(
        self,
        quality: str | None = None,
        band: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if quality:
            clauses.append("quality = ?")
            params.append(quality)
        if band:
            clauses.append("band_name = ?")
            params.append(band)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT * FROM transmissions
            {where}
            ORDER BY start_utc DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get(self, tx_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transmissions WHERE id = ?", (tx_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete(self, tx_id: int, remove_file: bool = True) -> bool:
        row = self.get(tx_id)
        if not row:
            return False
        with self._connect() as conn:
            conn.execute("DELETE FROM transmissions WHERE id = ?", (tx_id,))
            conn.commit()
        if remove_file and row.get("audio_file"):
            p = Path(row["audio_file"])
            if p.is_file():
                p.unlink(missing_ok=True)
        return True

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0]
            accepted = conn.execute(
                "SELECT COUNT(*) FROM transmissions WHERE quality = 'accepted' OR quality IS NULL"
            ).fetchone()[0]
            rejected = conn.execute(
                "SELECT COUNT(*) FROM transmissions WHERE quality = 'rejected'"
            ).fetchone()[0]
            bands = conn.execute(
                "SELECT band_name, COUNT(*) c FROM transmissions GROUP BY band_name ORDER BY c DESC"
            ).fetchall()
            top_freq = conn.execute(
                """
                SELECT ROUND(frequency_mhz, 4) mhz, COUNT(*) c
                FROM transmissions
                GROUP BY ROUND(frequency_mhz, 4)
                ORDER BY c DESC LIMIT 10
                """
            ).fetchall()
        return {
            "total": total,
            "accepted": accepted,
            "rejected": rejected,
            "bands": [{"name": r[0], "count": r[1]} for r in bands],
            "top_frequencies": [{"mhz": r[0], "count": r[1]} for r in top_freq],
        }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
