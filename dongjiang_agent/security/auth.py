"""Local identity, session and security-audit persistence for the Web app."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


ALLOWED_ROLES = {
    "admin",
    "sales",
    "credit",
    "finance",
    "legal",
    "director",
    "ceo",
}
ROLE_LABELS = {
    "admin": "系统管理员",
    "sales": "销售",
    "credit": "信用管理",
    "finance": "财务",
    "legal": "法务",
    "director": "市场总监",
    "ceo": "集团管理层",
}
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$")
PASSWORD_ITERATIONS = 240_000
SESSION_HOURS = 8
LOCK_FAILURES = 5
LOCK_MINUTES = 15


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="seconds")


def _password_digest(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    ).hex()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(row: sqlite3.Row) -> dict[str, Any]:
    roles = json.loads(str(row["roles_json"] or "[]"))
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "roles": roles,
        "role_labels": [ROLE_LABELS.get(item, item) for item in roles],
        "active": bool(row["active"]),
        "must_change_password": bool(row["must_change_password"]),
        "last_login_at": row["last_login_at"],
        "created_at": row["created_at"],
    }


class AuthStore:
    """SQLite-backed users and opaque sessions with no plaintext credentials."""

    def __init__(self, path: str | Path = "data/auth/auth.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                roles_json TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                last_login_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                csrf_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS security_audit (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                actor_id TEXT,
                username TEXT,
                target_type TEXT,
                target_id TEXT,
                outcome TEXT NOT NULL,
                remote_address TEXT,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_security_audit_created ON security_audit(created_at);
            """
        )
        self.connection.commit()

    @staticmethod
    def validate_password(password: str) -> None:
        if len(password) < 10:
            raise ValueError("密码至少需要10个字符。")
        if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            raise ValueError("密码必须同时包含字母和数字。")

    @staticmethod
    def validate_roles(roles: list[str] | tuple[str, ...]) -> list[str]:
        normalized = sorted({str(item).strip() for item in roles if str(item).strip()})
        unknown = set(normalized) - ALLOWED_ROLES
        if unknown:
            raise ValueError(f"不支持的角色：{', '.join(sorted(unknown))}")
        if not normalized:
            raise ValueError("请至少选择一个角色。")
        return normalized

    def setup_required(self) -> bool:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return not row or int(row["count"]) == 0

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        roles: list[str] | tuple[str, ...],
        must_change_password: bool = True,
        actor: dict[str, Any] | None = None,
        remote_address: str = "",
    ) -> dict[str, Any]:
        normalized_username = username.strip().lower()
        if not USERNAME_PATTERN.fullmatch(normalized_username):
            raise ValueError("用户名需为3-40位字母、数字、点、下划线或连字符。")
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("请填写姓名。")
        self.validate_password(password)
        normalized_roles = self.validate_roles(roles)
        salt = secrets.token_bytes(16)
        user_id = f"USR-{uuid4().hex[:12].upper()}"
        now = _iso()
        try:
            self.connection.execute(
                """
                INSERT INTO users (
                    user_id, username, display_name, password_salt, password_hash,
                    roles_json, active, must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    user_id,
                    normalized_username,
                    normalized_name,
                    salt.hex(),
                    _password_digest(password, salt),
                    json.dumps(normalized_roles),
                    int(must_change_password),
                    now,
                    now,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("该用户名已存在。") from exc
        self.audit(
            "user.created",
            actor=actor,
            target_type="user",
            target_id=user_id,
            detail={"username": normalized_username, "roles": normalized_roles},
            remote_address=remote_address,
        )
        return self.get_user(user_id) or {}

    def bootstrap_admin(
        self, username: str, display_name: str, password: str, remote_address: str = ""
    ) -> dict[str, Any]:
        if not self.setup_required():
            raise PermissionError("系统已经完成管理员初始化。")
        return self.create_user(
            username=username,
            display_name=display_name,
            password=password,
            roles=["admin"],
            must_change_password=False,
            remote_address=remote_address,
        )

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return _public_user(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM users ORDER BY active DESC, display_name, username"
        ).fetchall()
        return [_public_user(row) for row in rows]

    def update_user(
        self,
        user_id: str,
        *,
        roles: list[str] | None = None,
        active: bool | None = None,
        actor: dict[str, Any] | None = None,
        remote_address: str = "",
    ) -> dict[str, Any]:
        existing = self.get_user(user_id)
        if not existing:
            raise KeyError("用户不存在。")
        next_roles = self.validate_roles(roles) if roles is not None else existing["roles"]
        next_active = bool(active) if active is not None else bool(existing["active"])
        if existing["username"] == (actor or {}).get("username") and not next_active:
            raise ValueError("不能停用当前登录账号。")
        if "admin" in existing["roles"] and (
            not next_active or "admin" not in next_roles
        ):
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM users WHERE active = 1 AND roles_json LIKE '%\"admin\"%'"
            ).fetchone()
            if row and int(row["count"]) <= 1:
                raise ValueError("系统必须保留至少一个启用的管理员。")
        self.connection.execute(
            "UPDATE users SET roles_json = ?, active = ?, updated_at = ? WHERE user_id = ?",
            (json.dumps(next_roles), int(next_active), _iso(), user_id),
        )
        if not next_active:
            self.connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.connection.commit()
        self.audit(
            "user.updated",
            actor=actor,
            target_type="user",
            target_id=user_id,
            detail={"roles": next_roles, "active": next_active},
            remote_address=remote_address,
        )
        return self.get_user(user_id) or {}

    def reset_password(
        self,
        user_id: str,
        temporary_password: str,
        *,
        actor: dict[str, Any],
        remote_address: str = "",
    ) -> None:
        if not self.get_user(user_id):
            raise KeyError("用户不存在。")
        self.validate_password(temporary_password)
        salt = secrets.token_bytes(16)
        self.connection.execute(
            """
            UPDATE users SET password_salt = ?, password_hash = ?,
                must_change_password = 1, failed_attempts = 0, locked_until = NULL,
                updated_at = ? WHERE user_id = ?
            """,
            (
                salt.hex(),
                _password_digest(temporary_password, salt),
                _iso(),
                user_id,
            ),
        )
        self.connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.connection.commit()
        self.audit(
            "auth.password_reset",
            actor=actor,
            target_type="user",
            target_id=user_id,
            remote_address=remote_address,
        )

    def authenticate(
        self, username: str, password: str, remote_address: str = ""
    ) -> dict[str, Any] | None:
        normalized_username = username.strip().lower()
        row = self.connection.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (normalized_username,),
        ).fetchone()
        if not row:
            self.audit(
                "auth.login",
                username=normalized_username,
                outcome="failed",
                detail={"reason": "invalid_credentials"},
                remote_address=remote_address,
            )
            return None
        now = _utc_now()
        locked_until = str(row["locked_until"] or "")
        if locked_until and datetime.fromisoformat(locked_until) > now:
            self.audit(
                "auth.login",
                username=normalized_username,
                outcome="blocked",
                detail={"reason": "temporarily_locked"},
                remote_address=remote_address,
            )
            raise PermissionError("登录失败次数过多，请15分钟后再试。")
        expected = str(row["password_hash"])
        actual = _password_digest(password, bytes.fromhex(str(row["password_salt"])))
        if not bool(row["active"]) or not hmac.compare_digest(expected, actual):
            failures = int(row["failed_attempts"]) + 1
            next_lock = _iso(now + timedelta(minutes=LOCK_MINUTES)) if failures >= LOCK_FAILURES else None
            self.connection.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ?, updated_at = ? WHERE user_id = ?",
                (failures, next_lock, _iso(now), row["user_id"]),
            )
            self.connection.commit()
            self.audit(
                "auth.login",
                username=normalized_username,
                outcome="failed",
                detail={"reason": "invalid_credentials"},
                remote_address=remote_address,
            )
            return None
        self.connection.execute(
            """
            UPDATE users SET failed_attempts = 0, locked_until = NULL,
                last_login_at = ?, updated_at = ? WHERE user_id = ?
            """,
            (_iso(now), _iso(now), row["user_id"]),
        )
        self.connection.commit()
        user = self.get_user(str(row["user_id"])) or {}
        self.audit(
            "auth.login",
            actor=user,
            outcome="success",
            remote_address=remote_address,
        )
        return user

    def create_session(self, user_id: str) -> dict[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = _utc_now()
        self.connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(now),))
        self.connection.execute(
            """
            INSERT INTO sessions (
                session_hash, user_id, csrf_token, created_at, expires_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _token_digest(token),
                user_id,
                csrf,
                _iso(now),
                _iso(now + timedelta(hours=SESSION_HOURS)),
                _iso(now),
            ),
        )
        self.connection.commit()
        return {"token": token, "csrf_token": csrf}

    def session_user(self, token: str) -> tuple[dict[str, Any], str] | None:
        if not token:
            return None
        row = self.connection.execute(
            """
            SELECT s.csrf_token, s.expires_at, u.*
            FROM sessions s JOIN users u ON u.user_id = s.user_id
            WHERE s.session_hash = ?
            """,
            (_token_digest(token),),
        ).fetchone()
        if not row or not bool(row["active"]):
            return None
        now = _utc_now()
        if datetime.fromisoformat(str(row["expires_at"])) <= now:
            self.connection.execute(
                "DELETE FROM sessions WHERE session_hash = ?", (_token_digest(token),)
            )
            self.connection.commit()
            return None
        self.connection.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE session_hash = ?",
            (_iso(now), _token_digest(token)),
        )
        self.connection.commit()
        return _public_user(row), str(row["csrf_token"])

    def delete_session(self, token: str) -> None:
        if token:
            self.connection.execute(
                "DELETE FROM sessions WHERE session_hash = ?", (_token_digest(token),)
            )
            self.connection.commit()

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        actor: dict[str, Any],
        remote_address: str = "",
    ) -> None:
        row = self.connection.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            raise KeyError("用户不存在。")
        actual = _password_digest(
            current_password, bytes.fromhex(str(row["password_salt"]))
        )
        if not hmac.compare_digest(str(row["password_hash"]), actual):
            raise PermissionError("当前密码不正确。")
        self.validate_password(new_password)
        salt = secrets.token_bytes(16)
        self.connection.execute(
            """
            UPDATE users SET password_salt = ?, password_hash = ?,
                must_change_password = 0, updated_at = ? WHERE user_id = ?
            """,
            (salt.hex(), _password_digest(new_password, salt), _iso(), user_id),
        )
        self.connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.connection.commit()
        self.audit(
            "auth.password_changed",
            actor=actor,
            target_type="user",
            target_id=user_id,
            remote_address=remote_address,
        )

    def audit(
        self,
        event_type: str,
        *,
        actor: dict[str, Any] | None = None,
        username: str = "",
        target_type: str = "",
        target_id: str = "",
        outcome: str = "success",
        detail: dict[str, Any] | None = None,
        remote_address: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO security_audit (
                event_id, event_type, actor_id, username, target_type, target_id,
                outcome, remote_address, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"EVT-{uuid4().hex[:16].upper()}",
                event_type,
                (actor or {}).get("user_id"),
                (actor or {}).get("username") or username,
                target_type,
                target_id,
                outcome,
                remote_address,
                json.dumps(detail or {}, ensure_ascii=False),
                _iso(),
            ),
        )
        self.connection.commit()

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM security_audit ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "username": row["username"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "outcome": row["outcome"],
                "detail": json.loads(str(row["detail_json"] or "{}")),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "AuthStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
