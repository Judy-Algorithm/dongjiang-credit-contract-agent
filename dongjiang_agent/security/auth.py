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
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PASSWORD_ITERATIONS = 240_000
SESSION_HOURS = 8
LOCK_FAILURES = 5
LOCK_MINUTES = 15
RESET_CODE_MINUTES = 10
RESET_CODE_ATTEMPTS = 5
RESET_REQUEST_COOLDOWN_SECONDS = 60
RESET_REQUEST_WINDOW_MINUTES = 10
RESET_REQUESTS_PER_WINDOW = 5
REGISTRATIONS_PER_HOUR = 5
REGISTRATION_CODE_MINUTES = 10
REGISTRATION_CODE_ATTEMPTS = 5
REGISTRATION_REQUESTS_PER_WINDOW = 5
REGISTRATION_STATUSES = {"pending", "approved", "rejected"}


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
        "email": row["email"] or "",
        "registration_status": row["registration_status"] or "approved",
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
                email TEXT,
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
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "email" not in columns:
            self.connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
        self.connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
                ON users(email COLLATE NOCASE)
                WHERE email IS NOT NULL AND email != '';
            CREATE TABLE IF NOT EXISTS password_reset_codes (
                reset_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                code_salt TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                remote_address TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_password_reset_user_created
                ON password_reset_codes(user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS registration_verification_codes (
                verification_id TEXT PRIMARY KEY,
                email TEXT NOT NULL COLLATE NOCASE,
                code_salt TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                remote_address TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_registration_code_email_created
                ON registration_verification_codes(email, created_at DESC);
            CREATE TABLE IF NOT EXISTS notifications (
                notification_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                link TEXT,
                created_at TEXT NOT NULL,
                read_at TEXT,
                dedupe_key TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_user_created
                ON notifications(user_id, created_at DESC);
            """
        )
        if "registration_status" not in columns:
            self.connection.execute(
                "ALTER TABLE users ADD COLUMN registration_status TEXT NOT NULL DEFAULT 'approved'"
            )
        notification_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(notifications)"
            ).fetchall()
        }
        if "dedupe_key" not in notification_columns:
            self.connection.execute(
                "ALTER TABLE notifications ADD COLUMN dedupe_key TEXT"
            )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_user_dedupe
            ON notifications(user_id, dedupe_key)
            WHERE dedupe_key IS NOT NULL AND dedupe_key != ''
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
    def normalize_email(email: str, *, required: bool = True) -> str:
        normalized = email.strip().lower()
        if not normalized and not required:
            return ""
        if len(normalized) > 254 or not EMAIL_PATTERN.fullmatch(normalized):
            raise ValueError("请输入有效的邮箱地址。")
        return normalized

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
        email: str = "",
        active: bool = True,
        registration_status: str = "approved",
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
        normalized_email = self.normalize_email(email, required=False)
        normalized_roles = self.validate_roles(roles)
        normalized_registration = str(registration_status or "").strip().lower()
        if normalized_registration not in REGISTRATION_STATUSES:
            raise ValueError("注册状态无效。")
        salt = secrets.token_bytes(16)
        user_id = f"USR-{uuid4().hex[:12].upper()}"
        now = _iso()
        try:
            self.connection.execute(
                """
                INSERT INTO users (
                    user_id, username, display_name, email, password_salt, password_hash,
                    roles_json, active, registration_status, must_change_password,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    normalized_username,
                    normalized_name,
                    normalized_email or None,
                    salt.hex(),
                    _password_digest(password, salt),
                    json.dumps(normalized_roles),
                    int(active),
                    normalized_registration,
                    int(must_change_password),
                    now,
                    now,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名或邮箱已存在。") from exc
        self.audit(
            "user.created",
            actor=actor,
            target_type="user",
            target_id=user_id,
            detail={"username": normalized_username, "roles": normalized_roles},
            remote_address=remote_address,
        )
        return self.get_user(user_id) or {}

    def register_user(
        self,
        *,
        username: str,
        display_name: str,
        email: str,
        password: str,
        verification_code: str,
        remote_address: str = "",
    ) -> dict[str, Any]:
        if self.setup_required():
            raise PermissionError("请先完成系统管理员初始化。")
        normalized_username = username.strip().lower()
        if not USERNAME_PATTERN.fullmatch(normalized_username):
            raise ValueError("用户名需为3-40位字母、数字、点、下划线或连字符。")
        if not display_name.strip():
            raise ValueError("请填写姓名。")
        normalized_email = self.normalize_email(email)
        self.validate_password(password)
        duplicate = self.connection.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
            (normalized_username, normalized_email),
        ).fetchone()
        if duplicate:
            raise ValueError("用户名或邮箱已存在。")
        since = _iso(_utc_now() - timedelta(hours=1))
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM security_audit
            WHERE event_type = 'user.registered' AND remote_address = ? AND created_at >= ?
            """,
            (remote_address, since),
        ).fetchone()
        if row and int(row["count"]) >= REGISTRATIONS_PER_HOUR:
            raise PermissionError("注册请求过于频繁，请稍后再试。")
        self.consume_registration_verification_code(
            normalized_email, verification_code, remote_address
        )
        user = self.create_user(
            username=username,
            display_name=display_name,
            email=email,
            password=password,
            roles=["sales"],
            active=False,
            registration_status="pending",
            must_change_password=False,
            remote_address=remote_address,
        )
        self.audit(
            "user.registered",
            actor=user,
            target_type="user",
            target_id=str(user["user_id"]),
            detail={"role": "sales"},
            remote_address=remote_address,
        )
        self.notify_roles(
            ["admin"],
            category="registration",
            title="新的注册申请",
            body=f"{user['display_name']}（{user['username']}）已完成邮箱验证，等待审核。",
            link="/registrations",
        )
        return user

    def issue_registration_verification_code(
        self, email: str, remote_address: str = ""
    ) -> tuple[str, str] | None:
        normalized_email = self.normalize_email(email)
        if self.connection.execute(
            "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE", (normalized_email,)
        ).fetchone():
            raise ValueError("该邮箱已绑定账号。")
        now = _utc_now()
        window_start = _iso(now - timedelta(minutes=RESET_REQUEST_WINDOW_MINUTES))
        count = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM security_audit
            WHERE event_type = 'auth.registration_code_requested'
              AND remote_address = ? AND created_at >= ?
            """,
            (remote_address, window_start),
        ).fetchone()
        if count and int(count["count"]) >= REGISTRATION_REQUESTS_PER_WINDOW:
            raise PermissionError("验证码请求过于频繁，请稍后再试。")
        latest = self.connection.execute(
            """
            SELECT created_at FROM registration_verification_codes
            WHERE email = ? COLLATE NOCASE ORDER BY created_at DESC LIMIT 1
            """,
            (normalized_email,),
        ).fetchone()
        self.audit(
            "auth.registration_code_requested",
            target_type="email",
            outcome="accepted",
            remote_address=remote_address,
        )
        if latest and datetime.fromisoformat(str(latest["created_at"])) > now - timedelta(
            seconds=RESET_REQUEST_COOLDOWN_SECONDS
        ):
            return None
        self.connection.execute(
            """
            UPDATE registration_verification_codes SET consumed_at = ?
            WHERE email = ? COLLATE NOCASE AND consumed_at IS NULL
            """,
            (_iso(now), normalized_email),
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_bytes(16)
        self.connection.execute(
            """
            INSERT INTO registration_verification_codes (
                verification_id, email, code_salt, code_hash, attempts,
                created_at, expires_at, remote_address
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                f"VER-{uuid4().hex[:16].upper()}",
                normalized_email,
                salt.hex(),
                _password_digest(code, salt),
                _iso(now),
                _iso(now + timedelta(minutes=REGISTRATION_CODE_MINUTES)),
                remote_address,
            ),
        )
        self.connection.commit()
        return normalized_email, code

    def invalidate_latest_registration_code(self, email: str) -> None:
        normalized_email = self.normalize_email(email)
        row = self.connection.execute(
            """
            SELECT verification_id FROM registration_verification_codes
            WHERE email = ? COLLATE NOCASE AND consumed_at IS NULL
            ORDER BY created_at DESC LIMIT 1
            """,
            (normalized_email,),
        ).fetchone()
        if row:
            self.connection.execute(
                "UPDATE registration_verification_codes SET consumed_at = ? WHERE verification_id = ?",
                (_iso(), row["verification_id"]),
            )
            self.connection.commit()

    def consume_registration_verification_code(
        self, email: str, code: str, remote_address: str = ""
    ) -> None:
        normalized_email = self.normalize_email(email)
        normalized_code = code.strip()
        if not re.fullmatch(r"\d{6}", normalized_code):
            raise PermissionError("验证码无效或已过期。")
        row = self.connection.execute(
            """
            SELECT * FROM registration_verification_codes
            WHERE email = ? COLLATE NOCASE AND consumed_at IS NULL
            ORDER BY created_at DESC LIMIT 1
            """,
            (normalized_email,),
        ).fetchone()
        now = _utc_now()
        valid = bool(
            row
            and int(row["attempts"]) < REGISTRATION_CODE_ATTEMPTS
            and datetime.fromisoformat(str(row["expires_at"])) > now
        )
        if valid and row:
            actual = _password_digest(
                normalized_code, bytes.fromhex(str(row["code_salt"]))
            )
            valid = hmac.compare_digest(str(row["code_hash"]), actual)
        if not valid:
            if row:
                attempts = int(row["attempts"]) + 1
                consumed_at = _iso(now) if attempts >= REGISTRATION_CODE_ATTEMPTS else None
                self.connection.execute(
                    "UPDATE registration_verification_codes SET attempts = ?, consumed_at = ? WHERE verification_id = ?",
                    (attempts, consumed_at, row["verification_id"]),
                )
                self.connection.commit()
            self.audit(
                "auth.registration_code_confirmed",
                outcome="failed",
                detail={"reason": "invalid_or_expired_code"},
                remote_address=remote_address,
            )
            raise PermissionError("验证码无效或已过期。")
        self.connection.execute(
            "UPDATE registration_verification_codes SET consumed_at = ? WHERE verification_id = ?",
            (_iso(now), row["verification_id"]),
        )
        self.connection.commit()
        self.audit(
            "auth.registration_code_confirmed",
            outcome="success",
            remote_address=remote_address,
        )

    def invalidate_latest_password_reset_code(self, email: str) -> None:
        normalized_email = self.normalize_email(email)
        user = self.connection.execute(
            "SELECT user_id FROM users WHERE email = ? COLLATE NOCASE",
            (normalized_email,),
        ).fetchone()
        if not user:
            return
        row = self.connection.execute(
            """
            SELECT reset_id FROM password_reset_codes
            WHERE user_id = ? AND consumed_at IS NULL
            ORDER BY created_at DESC LIMIT 1
            """,
            (user["user_id"],),
        ).fetchone()
        if row:
            self.connection.execute(
                "UPDATE password_reset_codes SET consumed_at = ? WHERE reset_id = ?",
                (_iso(), row["reset_id"]),
            )
            self.connection.commit()

    def bootstrap_admin(
        self,
        username: str,
        display_name: str,
        password: str,
        remote_address: str = "",
        email: str = "",
    ) -> dict[str, Any]:
        if not self.setup_required():
            raise PermissionError("系统已经完成管理员初始化。")
        return self.create_user(
            username=username,
            display_name=display_name,
            password=password,
            email=email,
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
            """
            SELECT * FROM users WHERE registration_status = 'approved'
            ORDER BY active DESC, display_name, username
            """
        ).fetchall()
        return [_public_user(row) for row in rows]

    def list_registration_applications(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM users
            WHERE registration_status IN ('pending', 'rejected')
            ORDER BY CASE registration_status WHEN 'pending' THEN 0 ELSE 1 END,
                     created_at DESC
            """
        ).fetchall()
        return [_public_user(row) for row in rows]

    def review_registration(
        self,
        user_id: str,
        *,
        decision: str,
        roles: list[str] | None,
        actor: dict[str, Any],
        remote_address: str = "",
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            raise KeyError("注册申请不存在。")
        if str(row["registration_status"] or "") != "pending":
            raise ValueError("该注册申请已经处理。")
        normalized = decision.strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("审核决定必须是批准或拒绝。")
        next_roles = self.validate_roles(roles or ["sales"])
        active = normalized == "approve"
        status = "approved" if active else "rejected"
        self.connection.execute(
            """
            UPDATE users SET roles_json = ?, active = ?, registration_status = ?,
                failed_attempts = 0, locked_until = NULL, updated_at = ? WHERE user_id = ?
            """,
            (json.dumps(next_roles), int(active), status, _iso(), user_id),
        )
        self.connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.connection.commit()
        user = self.get_user(user_id) or {}
        title = "注册申请已通过" if active else "注册申请未通过"
        body = (
            f"管理员已批准账号 {user['username']}，现在可以登录系统。"
            if active
            else f"管理员未批准账号 {user['username']}，如有疑问请联系管理员。"
        )
        self.create_notification(
            user_id,
            category="registration",
            title=title,
            body=body,
            link="/login" if active else "",
        )
        self.audit(
            "user.registration_reviewed",
            actor=actor,
            target_type="user",
            target_id=user_id,
            detail={"decision": normalized, "roles": next_roles},
            remote_address=remote_address,
        )
        return user

    def update_user(
        self,
        user_id: str,
        *,
        roles: list[str] | None = None,
        active: bool | None = None,
        email: str | None = None,
        actor: dict[str, Any] | None = None,
        remote_address: str = "",
    ) -> dict[str, Any]:
        existing = self.get_user(user_id)
        if not existing:
            raise KeyError("用户不存在。")
        next_roles = self.validate_roles(roles) if roles is not None else existing["roles"]
        next_active = bool(active) if active is not None else bool(existing["active"])
        if existing.get("registration_status") != "approved" and next_active:
            raise ValueError("注册申请必须在审核中心批准后才能启用。")
        next_email = (
            self.normalize_email(email, required=True)
            if email is not None
            else str(existing.get("email") or "")
        )
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
        try:
            self.connection.execute(
                "UPDATE users SET roles_json = ?, active = ?, email = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(next_roles), int(next_active), next_email or None, _iso(), user_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该邮箱已被其他账号使用。") from exc
        if not next_active:
            self.connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.connection.commit()
        self.audit(
            "user.updated",
            actor=actor,
            target_type="user",
            target_id=user_id,
            detail={"roles": next_roles, "active": next_active, "email_updated": email is not None},
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

    def issue_password_reset_code(
        self, email: str, remote_address: str = ""
    ) -> tuple[str, str] | None:
        normalized_email = self.normalize_email(email)
        now = _utc_now()
        window_start = _iso(now - timedelta(minutes=RESET_REQUEST_WINDOW_MINUTES))
        remote_count = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM security_audit
            WHERE event_type = 'auth.password_reset_requested'
              AND remote_address = ? AND created_at >= ?
            """,
            (remote_address, window_start),
        ).fetchone()
        if remote_count and int(remote_count["count"]) >= RESET_REQUESTS_PER_WINDOW:
            raise PermissionError("验证码请求过于频繁，请稍后再试。")

        user = self.connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE AND active = 1",
            (normalized_email,),
        ).fetchone()
        self.audit(
            "auth.password_reset_requested",
            username=str(user["username"]) if user else "",
            target_type="user" if user else "email",
            target_id=str(user["user_id"]) if user else "",
            outcome="accepted",
            detail={"account_matched": bool(user)},
            remote_address=remote_address,
        )
        if not user:
            return None

        latest = self.connection.execute(
            """
            SELECT created_at FROM password_reset_codes
            WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (user["user_id"],),
        ).fetchone()
        if latest:
            created_at = datetime.fromisoformat(str(latest["created_at"]))
            if created_at > now - timedelta(seconds=RESET_REQUEST_COOLDOWN_SECONDS):
                return None

        self.connection.execute(
            """
            UPDATE password_reset_codes SET consumed_at = ?
            WHERE user_id = ? AND consumed_at IS NULL
            """,
            (_iso(now), user["user_id"]),
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_bytes(16)
        self.connection.execute(
            """
            INSERT INTO password_reset_codes (
                reset_id, user_id, code_salt, code_hash, attempts,
                created_at, expires_at, remote_address
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                f"RST-{uuid4().hex[:16].upper()}",
                user["user_id"],
                salt.hex(),
                _password_digest(code, salt),
                _iso(now),
                _iso(now + timedelta(minutes=RESET_CODE_MINUTES)),
                remote_address,
            ),
        )
        self.connection.commit()
        return normalized_email, code

    def reset_password_with_code(
        self,
        email: str,
        code: str,
        new_password: str,
        remote_address: str = "",
    ) -> None:
        normalized_email = self.normalize_email(email)
        normalized_code = code.strip()
        if not re.fullmatch(r"\d{6}", normalized_code):
            raise PermissionError("验证码无效或已过期。")
        self.validate_password(new_password)
        user = self.connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE AND active = 1",
            (normalized_email,),
        ).fetchone()
        reset = None
        if user:
            reset = self.connection.execute(
                """
                SELECT * FROM password_reset_codes
                WHERE user_id = ? AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (user["user_id"],),
            ).fetchone()
        now = _utc_now()
        valid = bool(
            reset
            and int(reset["attempts"]) < RESET_CODE_ATTEMPTS
            and datetime.fromisoformat(str(reset["expires_at"])) > now
        )
        if valid and reset:
            actual = _password_digest(
                normalized_code, bytes.fromhex(str(reset["code_salt"]))
            )
            valid = hmac.compare_digest(str(reset["code_hash"]), actual)
        if not valid:
            if reset:
                attempts = int(reset["attempts"]) + 1
                consumed_at = _iso(now) if attempts >= RESET_CODE_ATTEMPTS else None
                self.connection.execute(
                    "UPDATE password_reset_codes SET attempts = ?, consumed_at = ? WHERE reset_id = ?",
                    (attempts, consumed_at, reset["reset_id"]),
                )
                self.connection.commit()
            self.audit(
                "auth.password_reset_confirmed",
                username=str(user["username"]) if user else "",
                outcome="failed",
                detail={"reason": "invalid_or_expired_code"},
                remote_address=remote_address,
            )
            raise PermissionError("验证码无效或已过期。")

        salt = secrets.token_bytes(16)
        self.connection.execute(
            """
            UPDATE users SET password_salt = ?, password_hash = ?,
                must_change_password = 0, failed_attempts = 0, locked_until = NULL,
                updated_at = ? WHERE user_id = ?
            """,
            (salt.hex(), _password_digest(new_password, salt), _iso(now), user["user_id"]),
        )
        self.connection.execute(
            "UPDATE password_reset_codes SET consumed_at = ? WHERE reset_id = ?",
            (_iso(now), reset["reset_id"]),
        )
        self.connection.execute("DELETE FROM sessions WHERE user_id = ?", (user["user_id"],))
        self.connection.commit()
        self.audit(
            "auth.password_reset_confirmed",
            actor=_public_user(user),
            target_type="user",
            target_id=str(user["user_id"]),
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

    def create_notification(
        self,
        user_id: str,
        *,
        category: str,
        title: str,
        body: str,
        link: str = "",
        dedupe_key: str = "",
    ) -> dict[str, Any] | None:
        if not self.get_user(user_id):
            raise KeyError("通知接收人不存在。")
        notification_id = f"NTF-{uuid4().hex[:16].upper()}"
        created_at = _iso()
        try:
            self.connection.execute(
                """
                INSERT INTO notifications (
                    notification_id, user_id, category, title, body, link,
                    created_at, dedupe_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    user_id,
                    str(category or "system")[:40],
                    str(title).strip()[:160],
                    str(body).strip()[:1000],
                    str(link).strip()[:500] or None,
                    created_at,
                    str(dedupe_key).strip()[:300] or None,
                ),
            )
        except sqlite3.IntegrityError:
            if dedupe_key:
                return None
            raise
        self.connection.commit()
        return {
            "notification_id": notification_id,
            "category": category,
            "title": title,
            "body": body,
            "link": link,
            "created_at": created_at,
            "read_at": None,
        }

    def notify_roles(
        self,
        roles: list[str] | tuple[str, ...],
        *,
        category: str,
        title: str,
        body: str,
        link: str = "",
        exclude_user_id: str = "",
    ) -> list[dict[str, Any]]:
        users = self.users_for_roles(roles, exclude_user_id=exclude_user_id)
        for user in users:
            self.create_notification(
                str(user["user_id"]),
                category=category,
                title=title,
                body=body,
                link=link,
            )
        return users

    def users_for_roles(
        self,
        roles: list[str] | tuple[str, ...],
        *,
        exclude_user_id: str = "",
    ) -> list[dict[str, Any]]:
        targets = set(self.validate_roles(roles))
        rows = self.connection.execute(
            "SELECT * FROM users WHERE active = 1 AND registration_status = 'approved'"
        ).fetchall()
        users: list[dict[str, Any]] = []
        for row in rows:
            if str(row["user_id"]) == exclude_user_id:
                continue
            current = set(json.loads(str(row["roles_json"] or "[]")))
            if "admin" not in current and not current.intersection(targets):
                continue
            users.append(_public_user(row))
        return users

    def list_notifications(self, user_id: str, limit: int = 100) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT * FROM notifications WHERE user_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (user_id, max(1, min(limit, 200))),
        ).fetchall()
        items = [
            {
                "notification_id": row["notification_id"],
                "category": row["category"],
                "title": row["title"],
                "body": row["body"],
                "link": row["link"] or "",
                "created_at": row["created_at"],
                "read_at": row["read_at"],
            }
            for row in rows
        ]
        unread = self.connection.execute(
            "SELECT COUNT(*) AS count FROM notifications WHERE user_id = ? AND read_at IS NULL",
            (user_id,),
        ).fetchone()
        return {"items": items, "unread": int(unread["count"]) if unread else 0}

    def mark_notification_read(self, user_id: str, notification_id: str) -> None:
        cursor = self.connection.execute(
            """
            UPDATE notifications SET read_at = COALESCE(read_at, ?)
            WHERE notification_id = ? AND user_id = ?
            """,
            (_iso(), notification_id, user_id),
        )
        self.connection.commit()
        if cursor.rowcount == 0:
            raise KeyError("通知不存在。")

    def mark_all_notifications_read(self, user_id: str) -> None:
        self.connection.execute(
            "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE user_id = ?",
            (_iso(), user_id),
        )
        self.connection.commit()

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
