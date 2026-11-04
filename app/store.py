"""Small SQLite store for per-chat conversation state, undo actions and usage counts.

No message text, audio or images are stored: only short-lived working state, which
cleanup() removes once it expires.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

PENDING_TTL_SECONDS = 30 * 60
UNDO_TTL_SECONDS = 7 * 24 * 3600
PROPOSAL_TTL_SECONDS = 24 * 3600
USAGE_KEEP_SECONDS = 400 * 24 * 3600  # a little over a year, enough for /usage


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_state (
                    chat_id INTEGER PRIMARY KEY,
                    pending TEXT,
                    pending_at REAL,
                    last_event_id TEXT
                );
                CREATE TABLE IF NOT EXISTS llm_usage (
                    ts REAL NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS undo (
                    token TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    created REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS proposal (
                    chat_id INTEGER PRIMARY KEY,
                    token TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created REAL NOT NULL
                );
                """
            )

    # -- pending clarification --------------------------------------------
    def get_pending(self, chat_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT pending, pending_at FROM chat_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
        if not row or not row[0]:
            return None
        if time.time() - (row[1] or 0) > PENDING_TTL_SECONDS:
            self.clear_pending(chat_id)
            return None
        return json.loads(row[0])

    def set_pending(self, chat_id: int, pending: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO chat_state(chat_id, pending, pending_at) VALUES(?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET pending=excluded.pending, pending_at=excluded.pending_at",
                (chat_id, json.dumps(pending), time.time()),
            )

    def clear_pending(self, chat_id: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE chat_state SET pending=NULL, pending_at=NULL WHERE chat_id=? AND pending IS NOT NULL",
                (chat_id,),
            )
            return cur.rowcount > 0

    # -- last referenced event --------------------------------------------
    def get_last_event(self, chat_id: int) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT last_event_id FROM chat_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return row[0] if row else None

    def set_last_event(self, chat_id: int, event_id: str | None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO chat_state(chat_id, last_event_id) VALUES(?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET last_event_id=excluded.last_event_id",
                (chat_id, event_id),
            )

    # -- undo ---------------------------------------------------------------
    def add_undo(self, chat_id: int, action: dict) -> str:
        token = secrets.token_urlsafe(9)
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM undo WHERE created < ?", (time.time() - UNDO_TTL_SECONDS,)
            )
            self._db.execute(
                "INSERT INTO undo(token, chat_id, action, created) VALUES(?,?,?,?)",
                (token, chat_id, json.dumps(action), time.time()),
            )
        return token

    def take_undo(self, token: str) -> tuple[int, dict] | None:
        """Return (chat_id, action) and mark it used, or None if unknown/used/expired."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT chat_id, action, created, used FROM undo WHERE token=?", (token,)
            ).fetchone()
            if not row or row[3] or time.time() - row[2] > UNDO_TTL_SECONDS:
                return None
            self._db.execute("UPDATE undo SET used=1 WHERE token=?", (token,))
        return row[0], json.loads(row[1])

    # -- picture proposal awaiting confirmation -------------------------------
    def set_proposal(self, chat_id: int, data: dict, token: str | None = None) -> str:
        token = token or secrets.token_urlsafe(9)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO proposal(chat_id, token, data, created) VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET token=excluded.token, data=excluded.data, "
                "created=excluded.created",
                (chat_id, token, json.dumps(data), time.time()),
            )
        return token

    def get_proposal(self, chat_id: int) -> tuple[str, dict] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT token, data, created FROM proposal WHERE chat_id=?", (chat_id,)
            ).fetchone()
        if not row:
            return None
        if time.time() - row[2] > PROPOSAL_TTL_SECONDS:
            self.clear_proposal(chat_id)
            return None
        return row[0], json.loads(row[1])

    def clear_proposal(self, chat_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM proposal WHERE chat_id=?", (chat_id,)).rowcount > 0

    # -- housekeeping ---------------------------------------------------------
    def cleanup(self) -> dict[str, int]:
        """Delete expired state and old usage rows, then compact the file."""
        now = time.time()
        with self._lock, self._db:
            removed = {
                "undo": self._db.execute(
                    "DELETE FROM undo WHERE used=1 OR created < ?", (now - UNDO_TTL_SECONDS,)
                ).rowcount,
                "pending": self._db.execute(
                    "UPDATE chat_state SET pending=NULL, pending_at=NULL "
                    "WHERE pending IS NOT NULL AND pending_at < ?", (now - PENDING_TTL_SECONDS,)
                ).rowcount,
                "proposal": self._db.execute(
                    "DELETE FROM proposal WHERE created < ?", (now - PROPOSAL_TTL_SECONDS,)
                ).rowcount,
                "llm_usage": self._db.execute(
                    "DELETE FROM llm_usage WHERE ts < ?", (now - USAGE_KEEP_SECONDS,)
                ).rowcount,
            }
        with self._lock:
            self._db.execute("VACUUM")
        return removed

    def stats(self) -> dict[str, int]:
        with self._lock:
            counts = {
                t: self._db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("chat_state", "undo", "proposal", "llm_usage")
            }
        counts["bytes"] = self.path.stat().st_size
        return counts

    # -- LLM usage ------------------------------------------------------------
    def add_usage(self, model: str, input_tokens: int, output_tokens: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO llm_usage(ts, model, input_tokens, output_tokens) VALUES(?,?,?,?)",
                (time.time(), model, input_tokens, output_tokens),
            )

    def usage_since(self, since_ts: float) -> list[tuple[str, int, int, int]]:
        """[(model, calls, input_tokens, output_tokens)] since the given time."""
        with self._lock:
            return self._db.execute(
                "SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM llm_usage "
                "WHERE ts >= ? GROUP BY model ORDER BY COUNT(*) DESC",
                (since_ts,),
            ).fetchall()
