import asyncio
import json
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
        self._load_from_disk()
        # Count existing results as completed
        RESULTS_DIR.mkdir(exist_ok=True)
        self.completed_count = len(list(RESULTS_DIR.glob("*_results.json")))

    def _path(self, sid: str) -> Path:
        return SESSIONS_DIR / f"{sid}.json"

    # ------------------------------------------------------------------
    # Synchronous helpers (called in worker threads or at startup)
    # ------------------------------------------------------------------

    @staticmethod
    def _write_atomic(path: Path, data: str) -> None:
        """Write *data* to *path* atomically via a temp-file + rename.

        On POSIX ``tmp.replace(path)`` is an atomic rename — the
        destination either holds the old content or the new content,
        never a partial write.
        """
        tmp = path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(path)

    def _load_from_disk(self):
        for p in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                # Backward compat: old session files may lack 'created_at'
                data.setdefault("created_at", p.stat().st_mtime)
                # Skip sessions whose user already completed (server crashed
                # between writing results and deleting session)
                user_id = data.get("user_id")
                if user_id and (RESULTS_DIR / f"{user_id}_results.json").exists():
                    p.unlink()  # clean up stale session file
                    continue
                self._sessions[p.stem] = SessionData(**data)
            except Exception:
                pass
        self.in_progress_count = len(self._sessions)

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

        Searches in-memory sessions first, then falls back to scanning disk
        for sessions that weren't loaded (e.g. after a restart where the user
        hasn't resumed yet).
        """
        # In-memory lookup
        for sid, data in self._sessions.items():
            if data.user_id == user_id:
                return sid
        # Disk fallback
        for p in SESSIONS_DIR.glob("*.json"):
            if p.stem in self._sessions:
                continue  # already checked above
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("user_id") == user_id:
                    return p.stem
            except Exception:
                pass
        return None

    async def save(self, sid: str):
        if sid in self._sessions:
            await self._persist(sid)

    async def delete(self, sid: str):
        self._sessions.pop(sid, None)
        p = self._path(sid)
        if p.exists():
            await asyncio.to_thread(p.unlink)

    def restore_from_disk(self, sid: str) -> Optional[SessionData]:
        """Re-hydrate a session that exists on disk but not in memory (server restart)."""
        p = self._path(sid)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # Backward compat: old session files may lack 'created_at'
            data.setdefault("created_at", p.stat().st_mtime)
            self._sessions[sid] = SessionData(**data)
            return self._sessions[sid]
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _persist(self, sid: str):
        """Serialize *sid* to disk in a worker thread (non-blocking)."""
        path = self._path(sid)
        data = json.dumps(asdict(self._sessions[sid]), ensure_ascii=False)
        await asyncio.to_thread(self._write_atomic, path, data)
