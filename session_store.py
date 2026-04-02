from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class UserSession:
    session_id: str
    username: str
    created_at: float
    last_active: float
    device_info: str = ""
    remember_me: bool = False

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "username": self.username,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "device_info": self.device_info,
            "remember_me": self.remember_me,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserSession":
        return cls(
            session_id=data["session_id"],
            username=data["username"],
            created_at=data["created_at"],
            last_active=data["last_active"],
            device_info=data.get("device_info", ""),
            remember_me=data.get("remember_me", False),
        )


class SessionManager:
    def __init__(
        self,
        session_expire_hours: int = 24 * 7,
        remember_days: int = 30,
        storage_dir: Path = None,
        secret_key: str | None = None,
    ):
        self.sessions: Dict[str, UserSession] = {}
        self.user_sessions: Dict[str, List[str]] = {}
        self.lock = threading.RLock()
        self.expire_hours = session_expire_hours
        self.remember_hours = remember_days * 24
        self._storage_dir = storage_dir
        self.secret_key = secret_key or secrets.token_hex(32)

    @property
    def storage_dir(self) -> Path:
        if self._storage_dir is None:
            self._storage_dir = Path(__file__).resolve().parent / "sessions"
        if not self._storage_dir.exists():
            self._storage_dir.mkdir(parents=True, exist_ok=True)
        return self._storage_dir

    def _get_session_file(self, session_id: str) -> Path:
        return self.storage_dir / f"{session_id}.json"

    def _sign_data(self, data: str) -> str:
        return hmac.new(
            self.secret_key.encode("utf-8"),
            data.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _verify_signature(self, data: str, signature: str) -> bool:
        expected = self._sign_data(data)
        return hmac.compare_digest(expected, signature)

    def _load_session(self, session_id: str) -> Optional[UserSession]:
        try:
            file_path = self._get_session_file(session_id)
            if not file_path.exists():
                return None
            data = json.loads(file_path.read_text(encoding="utf-8"))
            signature = data.pop("_signature", None)
            if not signature:
                return None
            serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
            if not self._verify_signature(serialized, signature):
                return None
            return UserSession.from_dict(data)
        except Exception:
            return None

    def _save_session(self, session: UserSession):
        try:
            file_path = self._get_session_file(session.session_id)
            data = session.to_dict()
            serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
            data["_signature"] = self._sign_data(serialized)
            file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _delete_session_file(self, session_id: str):
        try:
            file_path = self._get_session_file(session_id)
            if file_path.exists():
                file_path.unlink()
        except Exception:
            pass

    def create_session(self, username: str, device_info: str = "", remember_me: bool = False) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        session = UserSession(
            session_id=session_id,
            username=username,
            created_at=now,
            last_active=now,
            device_info=device_info,
            remember_me=remember_me,
        )
        with self.lock:
            self.sessions[session_id] = session
            if username not in self.user_sessions:
                self.user_sessions[username] = []
            self.user_sessions[username].append(session_id)
        self._save_session(session)
        return session_id

    def get_session(self, session_id: str) -> Optional[UserSession]:
        with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                session = self._load_session(session_id)
                if session:
                    self.sessions[session_id] = session
                    if session.username not in self.user_sessions:
                        self.user_sessions[session.username] = []
                    if session_id not in self.user_sessions[session.username]:
                        self.user_sessions[session.username].append(session_id)

            if session:
                expire_hours = self.remember_hours if session.remember_me else self.expire_hours
                if time.time() - session.last_active > expire_hours * 3600:
                    self.delete_session(session_id)
                    return None
                if time.time() - session.last_active > 300:
                    session.last_active = time.time()
                    self._save_session(session)
            return session

    def delete_session(self, session_id: str):
        with self.lock:
            session = self.sessions.pop(session_id, None)
            if session and session.username in self.user_sessions:
                try:
                    self.user_sessions[session.username].remove(session_id)
                    if not self.user_sessions[session.username]:
                        del self.user_sessions[session.username]
                except ValueError:
                    pass
        self._delete_session_file(session_id)

    def delete_user_sessions(self, username: str):
        with self.lock:
            session_ids = self.user_sessions.pop(username, [])
            for sid in session_ids:
                self.sessions.pop(sid, None)
        for sid in session_ids:
            self._delete_session_file(sid)

    def get_user_session_count(self, username: str) -> int:
        with self.lock:
            return len(self.user_sessions.get(username, []))

    def cleanup_expired(self):
        now = time.time()
        expired = []
        try:
            for file_path in self.storage_dir.glob("*.json"):
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    signature = data.pop("_signature", None)
                    if not signature:
                        expired.append(file_path.stem)
                        continue
                    serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
                    if not self._verify_signature(serialized, signature):
                        expired.append(file_path.stem)
                        continue
                    expire_hours = self.remember_hours if data.get("remember_me") else self.expire_hours
                    if now - data.get("last_active", 0) > expire_hours * 3600:
                        expired.append(data["session_id"])
                except Exception:
                    expired.append(file_path.stem)
        except Exception:
            pass
        for sid in expired:
            self.delete_session(sid)

    def get_expire_seconds(self, remember_me: bool = False) -> int:
        hours = self.remember_hours if remember_me else self.expire_hours
        return int(hours * 3600)
