import json
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


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, SessionData] = {}
        SESSIONS_DIR.mkdir(exist_ok=True)
        self._load_from_disk()

    def _path(self, sid: str) -> Path:
        return SESSIONS_DIR / f"{sid}.json"

    def _load_from_disk(self):
        for p in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self._sessions[p.stem] = SessionData(**data)
            except Exception:
                pass

    def _persist(self, sid: str):
        self._path(sid).write_text(
            json.dumps(asdict(self._sessions[sid]), ensure_ascii=False),
            encoding="utf-8",
        )

    def create(self, user_id: str, test_cases: list, url_params: dict) -> str:
        sid = str(uuid.uuid4())
        self._sessions[sid] = SessionData(
            user_id=user_id,
            test_cases=test_cases,
            current_page=0,
            results=[],
            url_params=url_params,
        )
        self._persist(sid)
        return sid

    def get(self, sid: str) -> Optional[SessionData]:
        return self._sessions.get(sid)

    def save(self, sid: str):
        if sid in self._sessions:
            self._persist(sid)

    def delete(self, sid: str):
        self._sessions.pop(sid, None)
        p = self._path(sid)
        if p.exists():
            p.unlink()

    def restore_from_disk(self, sid: str) -> Optional[SessionData]:
        """Re-hydrate a session that exists on disk but not in memory (server restart)."""
        p = self._path(sid)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._sessions[sid] = SessionData(**data)
            return self._sessions[sid]
        except Exception:
            return None
