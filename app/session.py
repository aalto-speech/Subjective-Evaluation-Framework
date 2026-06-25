import asyncio
import json
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

SESSIONS_DIR = Path("sessions")


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
        SESSIONS_DIR.mkdir(exist_ok=True)
        self._load_from_disk()

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
                self._sessions[p.stem] = SessionData(**data)
            except Exception:
                pass

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

    def get(self, sid: str) -> Optional[SessionData]:
        return self._sessions.get(sid)

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
