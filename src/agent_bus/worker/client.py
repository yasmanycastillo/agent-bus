from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any
from pathlib import Path

import httpx

from agent_bus.config import get_config_dir
from agent_bus.security import AuthenticationError, async_bus_client, load_session
from agent_bus.core.sse import iter_sse_frames

logger = logging.getLogger("agent_bus.worker.client")


def worker_environment(agent_id: str, *, per_agent: bool = False) -> dict[str, str]:
    """Select one worker's credentials without sharing an administrator's session."""
    env = os.environ.copy()
    env["AGENT_BUS_AGENT_ID"] = agent_id
    config_dir = get_config_dir().resolve()
    env["AGENT_BUS_CONFIG_DIR"] = str(config_dir)
    if per_agent:
        path = config_dir / "credentials" / f"{agent_id}.json"
    else:
        path = env.get("AGENT_BUS_SESSION_FILE")
    development = (
        env.get("AGENT_BUS_ALLOW_UNSIGNED") == "1"
        and not env.get("AGENT_BUS_SESSION_FILE")
        and not (per_agent and path.exists())
    )
    if development:
        env.pop("AGENT_BUS_SESSION_FILE", None)
    else:
        # Validates project, identity and expiry before any process is spawned.
        load_session(agent_id, session_file=path)
        # Keep the final filename unresolved so load_session still rejects symlinks.
        selected = Path(path) if path else config_dir / "credentials" / f"{agent_id}.json"
        env["AGENT_BUS_SESSION_FILE"] = str(selected.absolute())
    return env

EventCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class BusEventClient:
    """Authenticated SSE replay with cursors committed after callback success."""

    def __init__(
        self, agent_id: str, bus_url: str = "http://localhost:8420",
        on_event: EventCallback | None = None, reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 30.0, cursor: str | None = None,
    ) -> None:
        if reconnect_initial_delay <= 0 or reconnect_max_delay < reconnect_initial_delay:
            raise ValueError("Reconnect delays must be positive and ordered")
        self.agent_id = agent_id
        self.bus_url = bus_url.rstrip("/")
        self.on_event = on_event
        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay
        self.last_event_id = cursor
        self._running = False
        self._stop_event = asyncio.Event()
        self._active_task: asyncio.Task | None = None
        self._progress = 0

    async def start(self) -> None:
        if self._running:
            raise RuntimeError("Event client already running")
        self._running = True
        self._stop_event.clear()
        delay = self.reconnect_initial_delay
        try:
            while self._running:
                progress = self._progress
                self._active_task = asyncio.create_task(self._consume_sse())
                try:
                    await self._active_task
                except asyncio.CancelledError:
                    if self._stop_event.is_set():
                        return
                    raise
                except AuthenticationError:
                    raise
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (401, 403, 422):
                        raise
                    logger.warning("SSE unavailable for '%s': %s", self.agent_id, exc)
                except Exception as exc:
                    logger.warning("SSE interrupted for '%s': %s", self.agent_id, exc)
                finally:
                    self._active_task = None
                if not self._running:
                    return
                # EOF is a disconnect too. Even empty successful streams must back off.
                if self._progress != progress:
                    delay = self.reconnect_initial_delay
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, self.reconnect_max_delay)
        finally:
            self._running = False
            if self._active_task is not None:
                self._active_task.cancel()
                await asyncio.gather(self._active_task, return_exceptions=True)
                self._active_task = None

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        # A callback can stop its own client; let that successful callback commit its ID.
        if self._active_task is not None and self._active_task is not asyncio.current_task():
            self._active_task.cancel()

    async def _reset(self, data: dict) -> None:
        cursor = data.get("cursor")
        if data.get("error") != "cursor_expired" or not isinstance(cursor, str) or not cursor:
            raise ValueError("Invalid SSE reset control")
        if self.on_event:
            await self.on_event({**data, "event": "reset"})
        self.last_event_id = cursor
        self._progress += 1

    async def _consume_sse(self) -> None:
        headers = {"Last-Event-ID": self.last_event_id} if self.last_event_id is not None else {}
        async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=None) as client:
            async with client.stream("GET", f"/inbox/{self.agent_id}/events", headers=headers) as response:
                if response.status_code == 410:
                    await response.aread()
                    await self._reset(response.json())
                    return
                response.raise_for_status()
                async for frame in iter_sse_frames(response.aiter_lines()):
                    if not self._running:
                        return
                    event = frame["event"]
                    if event == "reset":
                        await self._reset(json.loads(frame["data"]))
                        return
                    if event == "checkpoint":
                        checkpoint = json.loads(frame["data"])
                        cursor = frame["id"] or checkpoint.get("cursor")
                        if not isinstance(cursor, str) or not cursor:
                            raise ValueError("Invalid SSE checkpoint")
                        self.last_event_id = cursor
                        self._progress += 1
                        continue
                    payload = {**self._parse_data(frame["data"]), "event": event}
                    if self.on_event is not None:
                        await self.on_event(payload)
                    if frame["id"] is not None:
                        self.last_event_id = frame["id"]
                    self._progress += 1
                    if not self._running:
                        return

    @staticmethod
    def _parse_data(data: str) -> dict[str, Any]:
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"raw": data}


async def iter_bus_events(
    agent_id: str, bus_url: str = "http://localhost:8420", stop: asyncio.Event | None = None,
    cursor: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield replayed events; stop interrupts even a silent stream or reconnect delay.

    Cursor advancement waits until the consumer resumes after a yielded event.
    Closing the iterator leaves an unfinished callback uncommitted for replay.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)

    async def receive(event):
        accepted = asyncio.get_running_loop().create_future()
        await queue.put((event, accepted))
        await accepted

    client = BusEventClient(agent_id, bus_url=bus_url, on_event=receive, cursor=cursor)
    runner = asyncio.create_task(client.start())
    stopped = asyncio.create_task(stop.wait()) if stop is not None else None
    take = None
    try:
        while stop is None or not stop.is_set():
            take = asyncio.create_task(queue.get())
            waiting = {take, runner}
            if stopped is not None:
                waiting.add(stopped)
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if stopped is not None and stopped in done:
                return
            if take in done:
                event, accepted = take.result()
                take = None
                yield event
                if not accepted.done():
                    accepted.set_result(None)
            elif runner in done:
                await runner
                return
    finally:
        client.stop()
        runner.cancel()
        pending = [task for task in (take, stopped, runner) if task is not None]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
