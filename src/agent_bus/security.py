"""Local session provisioning and origin-bound bearer clients.

Only token hashes are stored by the hub. Credential files are OS-user secrets;
HTTP registration never creates credentials. Expiry values are UNIX seconds.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from agent_bus.config import get_bus_url, get_config_dir, load_config
from agent_bus.reputation.database import Database


class AuthenticationError(ValueError):
    """Missing, invalid or unusable session credentials (never includes secrets)."""


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise AuthenticationError("Invalid identity: use 1-128 letters, digits, dots, underscores or hyphens")
    return value


@dataclass(frozen=True)
class Principal:
    agent_id: str
    session_id: str
    project_id: str
    role: str
    expires_at: float

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class SessionStore:
    def __init__(self, db: Database, project_id: str = "default") -> None:
        self.db = db
        self.project_id = validate_id(project_id)

    async def create(self, agent_id: str, role: str = "agent", ttl_seconds: int = 86400) -> dict:
        validate_id(agent_id)
        if agent_id == "free":
            raise AuthenticationError("Agent identity is reserved")
        if role not in ("agent", "admin"):
            raise AuthenticationError("Invalid session role")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 2592000:
            raise AuthenticationError("Session TTL must be between 1 and 2592000 seconds")
        await self.db.bind_project(self.project_id)
        token = secrets.token_urlsafe(32)
        session_id = secrets.token_hex(16)
        expires_at = time.time() + ttl_seconds
        await self.db.conn.execute(
            "INSERT INTO sessions (session_id, token_hash, agent_id, project_id, role, expires_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (session_id, hashlib.sha256(token.encode()).hexdigest(), agent_id, self.project_id, role, expires_at),
        )
        await self.db.conn.commit()
        return dict(token=token, agent_id=agent_id, session_id=session_id,
                    project_id=self.project_id, role=role, expires_at=expires_at)

    async def authenticate(self, token: str) -> Principal:
        await self.db.bind_project(self.project_id)
        if not isinstance(token, str) or not 43 <= len(token) <= 256:
            raise AuthenticationError("Invalid session")
        rows = await self.db.conn.execute_fetchall(
            "SELECT agent_id, session_id, project_id, role, expires_at FROM sessions "
            "WHERE token_hash = ? AND project_id = ? AND revoked = 0 AND expires_at > ?",
            (hashlib.sha256(token.encode()).hexdigest(), self.project_id, time.time()),
        )
        if not rows:
            raise AuthenticationError("Invalid session")
        return Principal(**dict(rows[0]))

    async def revoke(self, session_id: str) -> bool:
        await self.db.bind_project(self.project_id)
        validate_id(session_id)
        rows = await self.db.conn.execute_fetchall(
            "UPDATE sessions SET revoked = 1 WHERE session_id = ? AND project_id = ? AND revoked = 0 RETURNING session_id",
            (session_id, self.project_id),
        )
        await self.db.conn.commit()
        return bool(rows)


def resolve_agent_id(agent_id: str | None = None) -> str | None:
    selected = agent_id or os.environ.get("AGENT_BUS_AGENT_ID") or os.environ.get("AGENT_ID")
    if not selected:
        path = get_config_dir() / "current_agent"
        if path.exists():
            selected = path.read_text().strip()
    return validate_id(selected) if selected else None


def _validate_session(session: dict, agent_id: str | None, project_id: str | None = None) -> dict:
    try:
        for key in ("agent_id", "session_id", "project_id"):
            validate_id(session[key])
        if agent_id and session["agent_id"] != agent_id:
            raise AuthenticationError("Session identity does not match requested agent")
        if session["project_id"] != (project_id if project_id is not None else load_config().bus.project_id):
            raise AuthenticationError("Session belongs to another project")
        if session["role"] not in ("agent", "admin"):
            raise AuthenticationError("Invalid session role")
        if not isinstance(session["token"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", session["token"]):
            raise AuthenticationError("Invalid session token")
        expiry = session["expires_at"]
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= time.time():
            raise AuthenticationError("Session expired")
    except (KeyError, TypeError, AttributeError):
        raise AuthenticationError("Malformed session credentials") from None
    return dict(session)


def load_session(agent_id: str | None = None, *, session_file: Path | None = None, project_id: str | None = None) -> dict:
    explicit = session_file or os.environ.get("AGENT_BUS_SESSION_FILE")
    selected = agent_id or os.environ.get("AGENT_BUS_AGENT_ID") or os.environ.get("AGENT_ID")
    selected = validate_id(selected) if selected else (None if explicit else resolve_agent_id())
    if not explicit and not selected:
        raise AuthenticationError("Select an agent or configure AGENT_BUS_SESSION_FILE")
    path = Path(explicit) if explicit else get_config_dir() / "credentials" / f"{selected}.json"
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise AuthenticationError("Session credential file must be owned by this user with mode 0600")
        with os.fdopen(fd, "r") as stream:
            fd = None
            session = json.load(stream)
    except (OSError, ValueError) as exc:
        if isinstance(exc, AuthenticationError):
            raise
        raise AuthenticationError("Cannot load session credentials; provision with agent-bus auth create") from None
    finally:
        if fd is not None:
            os.close(fd)
    return _validate_session(session, selected, project_id)


def _origin(url: httpx.URL) -> tuple:
    return url.scheme, url.host, url.port


def _client_options(agent_id: str | None, session: dict | None, kwargs: dict, asynchronous: bool, project_id: str | None = None) -> dict:
    options = dict(kwargs)
    options["base_url"] = get_bus_url(options.get("base_url"))
    project_id = validate_id(project_id if project_id is not None else load_config().bus.project_id)
    headers = httpx.Headers(options.pop("headers", None))
    headers["X-Agent-Bus-Project"] = project_id
    options["headers"] = headers
    if session is None and not os.environ.get("AGENT_BUS_SESSION_FILE") and os.environ.get("AGENT_BUS_ALLOW_UNSIGNED") == "1":
        hooks = {name: list(values) for name, values in options.pop("event_hooks", {}).items()}
        def project_guard(request):
            request.headers["X-Agent-Bus-Project"] = project_id
        async def async_project_guard(request):
            project_guard(request)
        hooks.setdefault("request", []).insert(0, async_project_guard if asynchronous else project_guard)
        options["event_hooks"] = hooks
        return options
    credential = _validate_session(session, agent_id or session.get("agent_id"), project_id) if session is not None else load_session(agent_id, project_id=project_id)
    # A loopback URL must not send its bearer token through an ambient proxy.
    options.setdefault("trust_env", False)
    base = httpx.URL(options["base_url"])
    try:
        loopback = base.host == "localhost" or ipaddress.ip_address(base.host).is_loopback
    except ValueError:
        loopback = False
    if base.scheme != "https" and not (base.scheme == "http" and loopback):
        raise AuthenticationError("Bearer clients require HTTPS or HTTP loopback")
    if base.username or base.password or not base.host:
        raise AuthenticationError("Invalid bus origin")
    if "auth" in options:
        raise AuthenticationError("Session clients cannot override authentication")
    options["auth"] = _BearerAuth(credential["token"])
    hooks = {name: list(values) for name, values in options.pop("event_hooks", {}).items()}

    def guard(request):
        request.headers["X-Agent-Bus-Project"] = project_id
        if _origin(request.url) != _origin(base):
            raise AuthenticationError("Refusing to forward session to another origin")
        if credential["expires_at"] <= time.time():
            raise AuthenticationError("Session expired")

    async def async_guard(request):
        guard(request)

    hooks.setdefault("request", []).insert(0, async_guard if asynchronous else guard)
    options["event_hooks"] = hooks
    return options


class _BearerAuth(httpx.Auth):
    def __init__(self, token: str):
        self._token = token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def sync_bus_client(agent_id: str | None = None, *, session: dict | None = None, project_id: str | None = None, **kwargs) -> httpx.Client:
    return httpx.Client(**_client_options(agent_id, session, kwargs, False, project_id))


def async_bus_client(agent_id: str | None = None, *, session: dict | None = None, project_id: str | None = None, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(**_client_options(agent_id, session, kwargs, True, project_id))
