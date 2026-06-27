import asyncio
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

SESSIONS_DIR = Path("sessions")
RESULTS_DIR = Path("results")


@dataclass
class SessionData:
    user_id: str
    test_cases: list
    current_page: int
    results: list
    url_params: dict
    ref_audio_played: bool = False
    target_audio_played: bool = False
    created_at: float = 0.0  # epoch timestamp; 0 means "unknown" (old sessions)


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, SessionData] = {}
        # Atomic slot tracking (all methods touching these are sync — no await
        # inside them — so asyncio's cooperative multitasking guarantees atomicity)
        self.completed_count: int = 0
        self.in_progress_count: int = 0
        SESSIONS_DIR.mkdir(exist_ok=True)

        # --- SQLite setup ---
        self._db_path = SESSIONS_DIR / "sessions.db"
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._db_lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            data_json  TEXT NOT NULL,
            created_at REAL NOT NULL
        )""")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)"
        )
        self._db.commit()

        self._load_from_disk()

        # Count existing results as completed
        RESULTS_DIR.mkdir(exist_ok=True)
        self.completed_count = len(list(RESULTS_DIR.glob("*_results.json")))

    # ------------------------------------------------------------------
    # Internal helpers (called from the main thread at startup, or from
    # the thread pool via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _load_from_disk(self):
        """Load all sessions from the SQLite database into memory."""
        rows = self._db.execute(
            "SELECT id, user_id, data_json FROM sessions"
        ).fetchall()
        for sid, user_id, data_json in rows:
            try:
                data = json.loads(data_json)
                # Skip sessions whose user already completed (server crashed
                # between writing results and deleting session)
                if (RESULTS_DIR / f"{user_id}_results.json").exists():
                    self._db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                    continue
                self._sessions[sid] = SessionData(**data)
            except Exception:
                pass
        self._db.commit()  # commit any DELETEs from stale-session cleanup
        self.in_progress_count = len(self._sessions)

    # ------------------------------------------------------------------
    # Thread-pool helpers — these run inside asyncio.to_thread()
    # ------------------------------------------------------------------

    def _db_upsert(self, sid: str, user_id: str, data_json: str, created_at: float):
        with self._db_lock:
            self._db.execute(
                "INSERT OR REPLACE INTO sessions (id, user_id, data_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, user_id, data_json, created_at),
            )
            self._db.commit()

    def _db_delete(self, sid: str):
        with self._db_lock:
            self._db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            self._db.commit()

    # ------------------------------------------------------------------
    # Public API — all FS writes go through a thread to stay non-blocking
    # ------------------------------------------------------------------

    async def create(self, user_id: str, test_cases: list, url_params: dict) -> str:
        sid = str(uuid.uuid4())
        self._sessions[sid] = SessionData(
            user_id=user_id,
            test_cases=test_cases,
            current_page=0,
            results=[],
            url_params=url_params,
            created_at=time.time(),
        )
        await self._persist(sid)
        return sid

    # ------------------------------------------------------------------
    # Slot tracking — all synchronous (no await) for atomicity under asyncio
    # ------------------------------------------------------------------

    def reserve_slot(self, cap: int) -> bool:
        """Try to reserve a participant slot. Returns True if one is available.

        Must be called *before* ``create()``.  Because this method contains no
        ``await``, asyncio's cooperative multitasking guarantees the check-
        and-increment is atomic — no two coroutines can squeeze through the
        same slot.
        """
        if self.completed_count + self.in_progress_count >= cap:
            return False
        self.in_progress_count += 1
        return True

    def mark_completed(self):
        """Call when a participant finishes the test and writes results."""
        self.completed_count += 1
        self.in_progress_count -= 1

    def mark_abandoned(self):
        """Call when a session is cleaned up without completing (expired, duplicate, etc.)."""
        self.in_progress_count -= 1

    # ------------------------------------------------------------------

    def get(self, sid: str) -> Optional[SessionData]:
        return self._sessions.get(sid)

    def find_by_user(self, user_id: str) -> Optional[str]:
        """Return the session ID for *user_id* if an active session exists.

        Searches in-memory sessions first, then falls back to SQLite for
        sessions that weren't loaded (e.g. after a restart where the user
        hasn't resumed yet).
        """
        # In-memory lookup
        for sid, data in self._sessions.items():
            if data.user_id == user_id:
                return sid
        # Disk fallback via indexed SQLite query
        try:
            row = self._db.execute(
                "SELECT id FROM sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row and row[0] not in self._sessions:
                return row[0]
        except Exception:
            pass
        return None

    async def save(self, sid: str):
        if sid in self._sessions:
            await self._persist(sid)

    async def delete(self, sid: str):
        self._sessions.pop(sid, None)
        await asyncio.to_thread(self._db_delete, sid)

    def restore_from_disk(self, sid: str) -> Optional[SessionData]:
        """Re-hydrate a session that exists in the DB but not in memory (server restart)."""
        try:
            row = self._db.execute(
                "SELECT data_json FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if not row:
                return None
            data = json.loads(row[0])
            self._sessions[sid] = SessionData(**data)
            return self._sessions[sid]
        except Exception:
            return None

    def close(self):
        """Close the database connection (called on server shutdown)."""
        with self._db_lock:
            self._db.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _persist(self, sid: str):
        """Serialize *sid* to the SQLite database in a worker thread (non-blocking)."""
        if sid not in self._sessions:
            return
        # Capture all values before dispatching to thread pool, so we don't
        # read self._sessions[sid] from a different thread.
        data_json = json.dumps(asdict(self._sessions[sid]), ensure_ascii=False)
        user_id = self._sessions[sid].user_id
        created_at = self._sessions[sid].created_at
        await asyncio.to_thread(self._db_upsert, sid, user_id, data_json, created_at)
