"""SQLite-backed voice metadata repository.

Plain stdlib sqlite3 (no ORM), mirroring the lightweight care_* services.
The DB path comes from settings.DB_PATH, resolved relative to the project
root so the repository works regardless of the caller's cwd.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import ROOT_DIR, settings
from app.schemas import VoiceCreate, VoiceOut, VoiceProvider, VoiceUpdate

SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL CHECK(provider IN ('cloud', 'local')),
    status TEXT NOT NULL DEFAULT 'draft',
    reference_path TEXT,
    local_prompt_path TEXT,
    cloud_speaker_id TEXT,
    reference_text TEXT,
    speed_ratio REAL DEFAULT 1.0,
    loudness_ratio REAL DEFAULT 1.0,
    pitch_ratio REAL DEFAULT 1.0,
    created_at TEXT,
    updated_at TEXT
)
"""


def _db_path(db_path: str | None = None) -> Path:
    raw = db_path or settings.DB_PATH
    path = Path(raw)
    return path if path.is_absolute() else ROOT_DIR / path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_voice(row: sqlite3.Row) -> VoiceOut:
    return VoiceOut(
        id=row["id"],
        name=row["name"],
        provider=row["provider"],
        status=row["status"],
        reference_path=row["reference_path"] or "",
        local_prompt_path=row["local_prompt_path"] or "",
        cloud_speaker_id=row["cloud_speaker_id"] or "",
        reference_text=row["reference_text"] or "",
        speed_ratio=row["speed_ratio"],
        loudness_ratio=row["loudness_ratio"],
        pitch_ratio=row["pitch_ratio"],
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


class VoiceRepository:
    """CRUD + enable/disable for voice records."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = _db_path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA)
            # Lightweight migration: add reference_text to DBs created before
            # the transcript feature landed (CREATE TABLE IF NOT EXISTS does
            # not alter existing tables).
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(voices)")}
            if "reference_text" not in cols:
                conn.execute("ALTER TABLE voices ADD COLUMN reference_text TEXT")

    def create(self, payload: VoiceCreate) -> VoiceOut:
        now = _now()
        voice_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO voices (
                    id, name, provider, status,
                    reference_path, local_prompt_path, cloud_speaker_id,
                    reference_text,
                    speed_ratio, loudness_ratio, pitch_ratio,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    voice_id,
                    payload.name,
                    payload.provider,
                    payload.reference_path,
                    payload.local_prompt_path,
                    payload.cloud_speaker_id,
                    payload.reference_text,
                    payload.speed_ratio,
                    payload.loudness_ratio,
                    payload.pitch_ratio,
                    now,
                    now,
                ),
            )
        created = self.get(voice_id)
        if created is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError("voice insert failed")
        return created

    def get(self, voice_id: str) -> VoiceOut | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM voices WHERE id = ?", (voice_id,)
            ).fetchone()
        return _row_to_voice(row) if row else None

    def list(self, provider: VoiceProvider | None = None) -> list[VoiceOut]:
        with self._connect() as conn:
            if provider is None:
                rows = conn.execute(
                    "SELECT * FROM voices ORDER BY created_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM voices WHERE provider = ? ORDER BY created_at",
                    (provider,),
                ).fetchall()
        return [_row_to_voice(row) for row in rows]

    def update(self, voice_id: str, payload: VoiceUpdate) -> VoiceOut | None:
        fields = payload.model_dump(exclude_none=True)
        if not fields:
            return self.get(voice_id)
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE voices SET {assignments} WHERE id = ?",
                (*fields.values(), voice_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get(voice_id)

    def enable(self, voice_id: str) -> VoiceOut | None:
        return self._set_status(voice_id, "active")

    def disable(self, voice_id: str) -> VoiceOut | None:
        return self._set_status(voice_id, "disabled")

    def _set_status(self, voice_id: str, status: str) -> VoiceOut | None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE voices SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), voice_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get(voice_id)

    def delete(self, voice_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM voices WHERE id = ?", (voice_id,)
            )
            return cursor.rowcount > 0
