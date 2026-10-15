"""CLI for the deterministic watchdog over the hub's queues."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import click

from agent_bus.config import get_config_dir
from agent_bus.worker.notifier import ExternalNotifier
from agent_bus.worker.watchdog import findings, new_since


def _integrator_running() -> bool | None:
    """None when no integrator was started here: closing reviews without it is allowed."""
    from agent_bus.cli.integrator_cmds import _pid

    pid_file = _pid()
    if not pid_file.exists():
        return None
    try:
        os.kill(int(pid_file.read_text()), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def _check(notifier: ExternalNotifier, review_minutes: float, stuck_minutes: float) -> int:
    from agent_bus.cli.main import _client

    with _client() as client:  # type: ignore[no-untyped-call]
        response = client.get("/tasks")
        response.raise_for_status()
        tasks = response.json()
    current = findings(tasks, datetime.now(timezone.utc), review_minutes=review_minutes,
                       stuck_minutes=stuck_minutes, integrator_running=_integrator_running())
    fresh = new_since(current, get_config_dir() / "watchdog.json")
    for finding in fresh:
        click.echo(f"[{finding.kind}] {finding.message}")
        asyncio.run(notifier.notify(f"agent-bus: {finding.kind}", finding.message, level="warning",
                                    metadata={"kind": finding.kind, "subject": finding.subject}))
    return len(fresh)


@click.command("watchdog")
@click.option("--once", is_flag=True, help="Revisar una vez y salir.")
@click.option("--interval", default=60.0, type=float, show_default=True, help="Segundos entre revisiones.")
@click.option("--review-minutes", default=30.0, type=float, show_default=True,
              help="Minutos en in_review antes de avisar.")
@click.option("--stuck-minutes", default=60.0, type=float, show_default=True,
              help="Minutos sin cambios en in_progress antes de avisar.")
@click.option("--webhook", "webhooks", multiple=True, help="URL de webhook (Slack, Discord…); repetible.")
@click.option("--desktop/--no-desktop", default=True, show_default=True, help="Notificación de escritorio.")
def watchdog(once: bool, interval: float, review_minutes: float, stuck_minutes: float,
             webhooks: tuple[str, ...], desktop: bool) -> None:
    """Avisar de revisiones atascadas, integrador caído, bloqueos y claims sin avance.

    Determinista, sin modelo. Cada hallazgo se avisa una vez hasta que desaparece.
    Telegram y tmux se activan con TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID y AGENT_BUS_TMUX_TARGET.
    """
    notifier = ExternalNotifier(enable_desktop=desktop, webhook_urls=list(webhooks))
    while True:
        try:
            _check(notifier, review_minutes, stuck_minutes)
        except click.ClickException:
            raise
        except Exception as exc:
            if once:
                raise click.ClickException(f"No pude revisar el hub: {exc}") from exc
            click.echo(f"No pude revisar el hub: {exc}", err=True)
        if once:
            return
        time.sleep(interval)
