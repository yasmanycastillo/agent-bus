from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("agent_bus.worker.notifier")


@dataclass
class NotificationResult:
    delivered: bool
    channels: list[str] = field(default_factory=list)
    error: str | None = None


class ExternalNotifier:
    """External notification dispatcher for autonomous multi-agent teams.
    Dispatches alerts to Desktop (notify-send/macOS) and Webhooks (Slack/Discord/Telegram).
    """

    def __init__(
        self,
        enable_desktop: bool = True,
        webhook_urls: list[str] | None = None,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
    ) -> None:
        self.enable_desktop = enable_desktop
        self.webhook_urls = webhook_urls or []
        self.telegram_token = telegram_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    async def notify(
        self,
        title: str,
        message: str,
        level: str = "info",  # info, warning, error, success
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        """Dispatches notification to all configured channels."""
        channels_succeeded: list[str] = []
        errors: list[str] = []

        # 1. Desktop Notification
        if self.enable_desktop:
            desktop_ok = await self._send_desktop(title, message, level)
            if desktop_ok:
                channels_succeeded.append("desktop")

        # 2. Generic HTTP Webhooks
        for url in self.webhook_urls:
            webhook_ok = await self._send_webhook(url, title, message, level, metadata)
            if webhook_ok:
                channels_succeeded.append(f"webhook:{url[:20]}")
            else:
                errors.append(f"Webhook failed for {url}")

        # 3. Telegram Bot
        if self.telegram_token and self.telegram_chat_id:
            tg_ok = await self._send_telegram(title, message, level)
            if tg_ok:
                channels_succeeded.append("telegram")
            else:
                errors.append("Telegram notification failed")

        return NotificationResult(
            delivered=len(channels_succeeded) > 0,
            channels=channels_succeeded,
            error="; ".join(errors) if errors else None,
        )

    async def notify_goal_completed(self, goal: str, summary: str = "") -> NotificationResult:
        return await self.notify(
            title="🎯 Objetivo Completado",
            message=f"Objetivo: {goal}\n{summary}".strip(),
            level="success",
        )

    async def notify_task_blocked(self, task_id: str, title: str, reason: str) -> NotificationResult:
        return await self.notify(
            title=f"⚠️ Tarea Bloqueada: {task_id}",
            message=f"{title}\nRazón: {reason}",
            level="warning",
            metadata={"task_id": task_id, "reason": reason},
        )

    async def _send_desktop(self, title: str, message: str, level: str) -> bool:
        """Sends native desktop notification via notify-send or osascript."""
        notify_send = shutil.which("notify-send")
        if notify_send:
            urgency = "critical" if level in ("error", "warning") else "normal"
            try:
                proc = await asyncio.create_subprocess_exec(
                    notify_send, "-u", urgency, "-a", "agent-bus", title, message
                )
                await proc.communicate()
                return proc.returncode == 0
            except Exception as exc:
                logger.debug(f"notify-send failed: {exc}")
                return False

        osascript = shutil.which("osascript")
        if osascript:
            script = f'display notification "{message}" with title "{title}" subtitle "agent-bus"'
            try:
                proc = await asyncio.create_subprocess_exec(osascript, "-e", script)
                await proc.communicate()
                return proc.returncode == 0
            except Exception as exc:
                logger.debug(f"osascript failed: {exc}")
                return False

        return False

    async def _send_webhook(
        self,
        url: str,
        title: str,
        message: str,
        level: str,
        metadata: dict[str, Any] | None,
    ) -> bool:
        payload = {
            "source": "agent-bus",
            "title": title,
            "message": message,
            "level": level,
            "metadata": metadata or {},
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                return resp.status_code in (200, 201, 204)
        except Exception as exc:
            logger.warning(f"Webhook error for {url}: {exc}")
            return False

    async def _send_telegram(self, title: str, message: str, level: str) -> bool:
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        text = f"<b>{title}</b>\n\n{message}"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                return resp.status_code == 200
        except Exception as exc:
            logger.warning(f"Telegram error: {exc}")
            return False
