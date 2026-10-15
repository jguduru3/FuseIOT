"""
User accounts, roles and tokens for the FuseIoT server.

Roles (each includes the ones before it):

* ``viewer``   - see devices, state, history, rules and schedules
* ``operator`` - also run device commands and trigger schedules
* ``admin``    - also enable/disable rules and schedules, manage users,
                 send test notifications

Passwords are hashed with scrypt; tokens are random 256-bit values of which
only a SHA-256 hash is stored. Login sessions expire (30 days by default);
API tokens (for scripts, phones, other systems) last until revoked.
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .exceptions import AuthenticationError, AuthorizationError, ConfigurationError
from .logging_config import get_logger

logger = get_logger("auth")

ROLES = ("viewer", "operator", "admin")
_USERNAME = re.compile(r"^[A-Za-z0-9_.\-@]{1,64}$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r),
                                p=int(p), dklen=len(digest_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def role_allows(role: str, required: str) -> bool:
    return ROLES.index(role) >= ROLES.index(required)


@dataclass
class Principal:
    """Who is making a request."""
    username: str
    role: str
    token_kind: str  # "session" | "api" | "service" | "anonymous"

    def can(self, required: str) -> bool:
        return role_allows(self.role, required)

    def to_dict(self) -> Dict[str, Any]:
        return {"username": self.username, "role": self.role, "token_kind": self.token_kind}


class LoginThrottle:
    """Slow down password guessing: ``max_failures`` per ``window`` seconds per client."""

    def __init__(self, max_failures: int = 5, window: float = 300.0):
        self.max_failures = max_failures
        self.window = window
        self._failures: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> List[float]:
        attempts = [t for t in self._failures.get(key, []) if now - t < self.window]
        self._failures[key] = attempts
        return attempts

    def blocked_for(self, key: str) -> float:
        now = time.monotonic()
        with self._lock:
            attempts = self._recent(key, now)
            if len(attempts) < self.max_failures:
                return 0.0
            return max(0.0, self.window - (now - attempts[0]))

    def failed(self, key: str) -> None:
        with self._lock:
            self._recent(key, time.monotonic()).append(time.monotonic())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class UserStore:
    """SQLite-backed users and tokens."""

    def __init__(self, path: str, session_ttl: float = 30 * 24 * 3600):
        self.path = path
        self.session_ttl = session_ttl
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at REAL NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL,
                last_used REAL
            );
            CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(username);
            """
        )
        self._conn.commit()

    # --------------------------------------------------------------- users

    def has_users(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def add_user(self, username: str, password: str, role: str = "viewer") -> None:
        if not _USERNAME.match(username):
            raise ConfigurationError("Username may contain letters, digits and . _ - @ (max 64)")
        if role not in ROLES:
            raise ConfigurationError(f"Role must be one of {', '.join(ROLES)}")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ConfigurationError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                    (username, hash_password(password), role, time.time()),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                raise ConfigurationError(f"User already exists: {username}") from None
        logger.info("user_added", username=username, role=role)

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, role, created_at, disabled FROM users ORDER BY username").fetchall()
        return [{"username": u, "role": r, "created_at": c, "disabled": bool(d)} for u, r, c, d in rows]

    def _require_user(self, username: str) -> Tuple[str, str, int]:
        row = self._conn.execute(
            "SELECT password_hash, role, disabled FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            raise ConfigurationError(f"No such user: {username}")
        return row

    def _admins_left_without(self, username: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND username != ?",
            (username,)).fetchone()[0]

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise ConfigurationError(f"Role must be one of {', '.join(ROLES)}")
        with self._lock:
            _, current, _ = self._require_user(username)
            if current == "admin" and role != "admin" and self._admins_left_without(username) == 0:
                raise ConfigurationError("Refusing to demote the last admin")
            self._conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
            self._conn.commit()

    def set_password(self, username: str, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ConfigurationError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        with self._lock:
            self._require_user(username)
            self._conn.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                               (hash_password(password), username))
            # A password change signs the user out everywhere.
            self._conn.execute("DELETE FROM tokens WHERE username = ? AND kind = 'session'", (username,))
            self._conn.commit()

    def set_disabled(self, username: str, disabled: bool) -> None:
        with self._lock:
            _, role, _ = self._require_user(username)
            if disabled and role == "admin" and self._admins_left_without(username) == 0:
                raise ConfigurationError("Refusing to disable the last admin")
            self._conn.execute("UPDATE users SET disabled = ? WHERE username = ?", (int(disabled), username))
            if disabled:
                self._conn.execute("DELETE FROM tokens WHERE username = ?", (username,))
            self._conn.commit()

    def remove_user(self, username: str) -> None:
        with self._lock:
            _, role, _ = self._require_user(username)
            if role == "admin" and self._admins_left_without(username) == 0:
                raise ConfigurationError("Refusing to remove the last admin")
            self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
            self._conn.commit()

    # -------------------------------------------------------------- tokens

    def authenticate(self, username: str, password: str) -> Principal:
        with self._lock:
            row = self._conn.execute(
                "SELECT password_hash, role, disabled FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            verify_password(password, hash_password("timing-equaliser"))
            raise AuthenticationError("Invalid username or password")
        password_hash, role, disabled = row
        if not verify_password(password, password_hash) or disabled:
            raise AuthenticationError("Invalid username or password")
        return Principal(username, role, "session")

    def issue_token(self, username: str, name: str = "session", kind: str = "session",
                    ttl: Optional[float] = None) -> Tuple[str, Optional[float]]:
        """Create a token. Returns (token, expires_at). The token is shown only once."""
        if kind not in ("session", "api"):
            raise ValueError("kind must be 'session' or 'api'")
        token = "fio_" + secrets.token_urlsafe(32)
        now = time.time()
        if ttl is None and kind == "session":
            ttl = self.session_ttl
        expires = now + ttl if ttl else None
        with self._lock:
            self._require_user(username)
            self._conn.execute(
                "INSERT INTO tokens (token_hash, username, name, kind, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (_token_hash(token), username, name[:64], kind, now, expires))
            self._conn.execute("DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
            self._conn.commit()
        return token, expires

    def resolve_token(self, token: str) -> Optional[Principal]:
        if not token.startswith("fio_"):
            return None
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT t.username, u.role, t.kind, t.expires_at, u.disabled, t.last_used FROM tokens t "
                "JOIN users u ON u.username = t.username WHERE t.token_hash = ?",
                (_token_hash(token),)).fetchone()
            if row is None:
                return None
            username, role, kind, expires, disabled, last_used = row
            if disabled or (expires is not None and expires < now):
                return None
            # Record usage at most once a minute: a write per request is slow on SD cards.
            if last_used is None or now - last_used > 60:
                self._conn.execute("UPDATE tokens SET last_used = ? WHERE token_hash = ?",
                                   (now, _token_hash(token)))
                self._conn.commit()
        return Principal(username, role, kind)

    def revoke_token(self, token: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM tokens WHERE token_hash = ?", (_token_hash(token),))
            self._conn.commit()
            return cur.rowcount > 0

    def list_tokens(self, username: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(token_hash, 1, 12), name, kind, created_at, expires_at, last_used FROM tokens "
                "WHERE username = ? AND kind = 'api' ORDER BY created_at", (username,)).fetchall()
        return [{"id": i, "name": n, "kind": k, "created_at": c, "expires_at": e, "last_used": lu}
                for i, n, k, c, e, lu in rows]

    def revoke_token_id(self, username: str, token_id: str) -> bool:
        """Revoke one of ``username``'s API tokens by the id shown in :meth:`list_tokens`."""
        token_id = token_id.lower()
        if not re.fullmatch(r"[0-9a-f]{12}", token_id):
            return False
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM tokens WHERE username = ? AND kind = 'api' AND substr(token_hash, 1, 12) = ?",
                (username, token_id))
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Authenticator:
    """Resolves request credentials to a :class:`Principal`."""

    def __init__(self, users: Optional[UserStore] = None, service_token: Optional[str] = None):
        self.users = users
        self.service_token = service_token
        self.throttle = LoginThrottle()

    @property
    def enabled(self) -> bool:
        """True when requests must authenticate."""
        return bool(self.service_token) or (self.users is not None and self.users.has_users())

    def resolve(self, token: Optional[str]) -> Principal:
        """
        Map a bearer token to a principal.

        Raises:
            AuthenticationError: if auth is enabled and the token is missing/invalid.
        """
        if not self.enabled:
            return Principal("anonymous", "admin", "anonymous")
        if token:
            if self.service_token and hmac.compare_digest(token.encode(), self.service_token.encode()):
                return Principal("service", "admin", "service")
            if self.users is not None:
                principal = self.users.resolve_token(token)
                if principal is not None:
                    return principal
        raise AuthenticationError("Missing or invalid credentials")

    def login(self, username: str, password: str, client: str) -> Tuple[str, Optional[float], Principal]:
        if self.users is None:
            raise ConfigurationError("User accounts are not enabled (set users_db in the config)")
        wait = self.throttle.blocked_for(client)
        if wait > 0:
            raise AuthorizationError(f"Too many failed logins; try again in {int(wait) + 1}s",
                                     details={"retry_after": int(wait) + 1})
        try:
            principal = self.users.authenticate(username, password)
        except AuthenticationError:
            self.throttle.failed(client)
            logger.warning("login_failed", username=username, client=client)
            raise
        self.throttle.succeeded(client)
        token, expires = self.users.issue_token(principal.username, name="login", kind="session")
        logger.info("login", username=username, client=client)
        return token, expires, principal
