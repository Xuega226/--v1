"""Append-only, privacy-aware ledger of activities the bot actually observed."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import json
import os
import sqlite3
import time
import uuid
from typing import Any


_PRIVACY_LEVEL = {"private": 0, "relationship": 1, "public": 2}


@dataclass(frozen=True)
class ActivityEvent:
    event_id: str
    occurred_at: float
    kind: str
    actor_scope: str
    summary: str
    details: dict[str, Any]
    privacy: str
    verified: bool
    source: str
    significance: float
    emotional_valence: float
    shareable: bool
    shared_at: float
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActivityLedger:
    """SQLite event ledger.

    Records are immutable after insertion, except for ``shared_at`` which is a
    delivery receipt used to prevent the same public experience being posted
    repeatedly. Callers should store summaries, never raw private messages.
    """

    def __init__(self, path: str, *, enabled: bool = True):
        self.path = os.path.abspath(path)
        self.enabled = bool(enabled)
        if self.enabled:
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    actor_scope TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    privacy TEXT NOT NULL,
                    verified INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    significance REAL NOT NULL,
                    emotional_valence REAL NOT NULL,
                    shareable INTEGER NOT NULL,
                    shared_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_occurred "
                "ON activity_events(occurred_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_public "
                "ON activity_events(privacy, verified, shareable, shared_at, occurred_at DESC)"
            )

    def record(
        self,
        *,
        kind: str,
        summary: str,
        actor_scope: str = "self",
        details: dict[str, Any] | None = None,
        privacy: str = "private",
        verified: bool = True,
        source: str = "runtime",
        significance: float = 0.5,
        emotional_valence: float = 0.0,
        shareable: bool = False,
        occurred_at: float | None = None,
        event_id: str = "",
    ) -> str:
        if not self.enabled:
            return ""
        privacy = privacy if privacy in _PRIVACY_LEVEL else "private"
        # Public sharing must be explicit and based on an actually observed event.
        shareable = bool(shareable and verified and privacy == "public")
        event_id = str(event_id or uuid.uuid4().hex)
        summary = " ".join(str(summary or "").split())[:240]
        if not summary:
            raise ValueError("activity summary cannot be empty")
        occurred_at = time.time() if occurred_at is None else float(occurred_at)
        safe_details = self._safe_details(details or {})
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO activity_events (
                    event_id, occurred_at, kind, actor_scope, summary, details_json,
                    privacy, verified, source, significance, emotional_valence,
                    shareable, shared_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    event_id,
                    occurred_at,
                    str(kind or "activity")[:80],
                    str(actor_scope or "self")[:80],
                    summary,
                    json.dumps(safe_details, ensure_ascii=False, separators=(",", ":")),
                    privacy,
                    int(bool(verified)),
                    str(source or "runtime")[:80],
                    self._clamp(significance, 0.0, 1.0),
                    self._clamp(emotional_valence, -1.0, 1.0),
                    int(shareable),
                    time.time(),
                ),
            )
        return event_id

    def recent(self, limit: int = 20, *, minimum_privacy: str | None = None) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        limit = max(1, min(200, int(limit)))
        query = "SELECT * FROM activity_events"
        params: list[Any] = []
        if minimum_privacy in _PRIVACY_LEVEL:
            allowed = [name for name, level in _PRIVACY_LEVEL.items() if level >= _PRIVACY_LEVEL[minimum_privacy]]
            query += f" WHERE privacy IN ({','.join('?' for _ in allowed)})"
            params.extend(allowed)
        query += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def public_candidates(self, limit: int = 5, *, since: float = 0.0) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM activity_events
                WHERE privacy = 'public' AND verified = 1 AND shareable = 1
                  AND shared_at = 0 AND occurred_at >= ?
                ORDER BY significance DESC, occurred_at DESC
                LIMIT ?
                """,
                (float(since), max(1, min(50, int(limit)))),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def mark_shared(self, event_id: str, *, shared_at: float | None = None) -> bool:
        if not self.enabled or not event_id:
            return False
        shared_at = time.time() if shared_at is None else float(shared_at)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE activity_events SET shared_at = ?
                WHERE event_id = ? AND privacy = 'public' AND verified = 1 AND shareable = 1
                """,
                (shared_at, str(event_id)),
            )
        return cursor.rowcount > 0

    def stats(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "total": 0, "public_unshared": 0, "latest_at": 0.0}
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN privacy='public' AND verified=1 AND shareable=1
                                     AND shared_at=0 THEN 1 ELSE 0 END) AS public_unshared,
                       MAX(occurred_at) AS latest_at
                FROM activity_events
                """
            ).fetchone()
        return {
            "enabled": True,
            "total": int(row["total"] or 0),
            "public_unshared": int(row["public_unshared"] or 0),
            "latest_at": float(row["latest_at"] or 0.0),
        }

    @staticmethod
    def _safe_details(details: dict[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, value in list(details.items())[:20]:
            key = str(key)[:60]
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value[:240] if isinstance(value, str) else value
        return safe

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json"))
        except (TypeError, json.JSONDecodeError):
            item["details"] = {}
            item.pop("details_json", None)
        item["verified"] = bool(item["verified"])
        item["shareable"] = bool(item["shareable"])
        return item

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, float(value)))
