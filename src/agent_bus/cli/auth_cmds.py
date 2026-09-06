"""Explicit local operator session provisioning; no HTTP bootstrap endpoint."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import click

from agent_bus.config import get_config_dir, load_config
from agent_bus.reputation.database import Database, ProjectMismatchError
from agent_bus.security import AuthenticationError, SessionStore, validate_id


@click.group()
def auth():
    """Provision/revoke local project credentials as the OS operator."""


@auth.command("create")
@click.option("--agent", help="Explicit operational participant identity; reuse to rotate its credentials.")
@click.option("--provider", help="Provider label; generates a unique agent identity when --agent is omitted.")
@click.option("--role", type=click.Choice(["agent", "admin"]), default="agent", show_default=True)
@click.option("--ttl", type=click.IntRange(1, 2592000), default=86400, show_default=True)
@click.option("--output", type=click.Path(path_type=Path))
def create(agent: str | None, provider: str | None, role: str, ttl: int, output: Path | None):
    """Create a session file (0600); refuses to overwrite existing credentials."""
    try:
        if provider is not None:
            validate_id(provider)
        if agent is None:
            if provider is None:
                raise AuthenticationError("Provide --agent or --provider")
            agent = f"{provider[:115]}-{uuid.uuid4().hex[:12]}"
        validate_id(agent)
        target = output or get_config_dir() / "credentials" / f"{agent}.json"
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Reserve without following symlinks or replacing another session.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except (OSError, AuthenticationError) as exc:
        raise click.ClickException(f"Cannot create credential file: {exc}") from None

    async def provision():
        config = load_config()
        db = Database(config.database_path, project_id=config.bus.project_id)
        await db.initialize()
        try:
            store = SessionStore(db, config.bus.project_id)
            session = await store.create(agent, role, ttl)
            if provider is not None:
                session["provider"] = provider
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(session, stream)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                await store.revoke(session["session_id"])
                raise
            return session
        finally:
            await db.close()

    try:
        session = asyncio.run(provision())
    except BaseException as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        target.unlink(missing_ok=True)
        if isinstance(exc, (AuthenticationError, ProjectMismatchError)):
            raise click.ClickException(str(exc)) from None
        raise
    click.echo(f"Session {session['session_id']} created for {agent} ({role})")
    click.echo(f"Credentials: {target}; expires at UNIX {session['expires_at']:.0f}")


@auth.command("revoke")
@click.option("--session", "session_id", required=True)
def revoke(session_id: str):
    """Revoke a session in this project's database."""
    async def perform():
        config = load_config()
        db = Database(config.database_path, project_id=config.bus.project_id)
        await db.initialize()
        try:
            return await SessionStore(db, config.bus.project_id).revoke(session_id)
        finally:
            await db.close()

    try:
        revoked = asyncio.run(perform())
    except (AuthenticationError, ProjectMismatchError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo("Session revoked" if revoked else "Session absent or already revoked")
