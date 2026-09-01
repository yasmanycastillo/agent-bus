from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import httpx

logger = logging.getLogger("agent_bus.worker.client")

EventCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class BusEventClient:
    """Cliente de eventos del bus (SSE con fallback a polling) con reconexión automática.

    Escucha ``GET /events/{agent_id}`` (stream SSE). Ante desconexión o error,
    reintenta con backoff exponencial y re-notifica su presencia al bus.
    """

    def __init__(
        self,
        agent_id: str,
        bus_url: str = "http://localhost:8420",
        on_event: EventCallback | None = None,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
    ) -> None:
        self.agent_id = agent_id
        self.bus_url = bus_url.rstrip("/")
        self.on_event = on_event
        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay
        self._running = False

    async def start(self) -> None:
        """Corre el loop de escucha hasta que se llame a stop()."""
        self._running = True
        delay = self.reconnect_initial_delay
        while self._running:
            try:
                await self._consume_sse()
                delay = self.reconnect_initial_delay  # conexión limpia: reset
            except Exception:
                if not self._running:
                    return
                logger.warning(
                    "SSE connection lost for '%s'; reconnecting in %.1fs",
                    self.agent_id,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max_delay)

    def stop(self) -> None:
        self._running = False

    async def _consume_sse(self) -> None:
        """Una sesión SSE completa; termina si la conexión se corta o se hace stop()."""
        async with httpx.AsyncClient(base_url=self.bus_url, timeout=None) as client:
            async with client.stream("GET", f"/events/{self.agent_id}") as resp:
                resp.raise_for_status()
                logger.info("SSE connected for '%s'", self.agent_id)
                event: dict[str, Any] | None = None
                async for line in resp.aiter_lines():
                    if not self._running:
                        return
                    if line.startswith("event:"):
                        event = {"event": line[len("event:") :].strip()}
                    elif line.startswith("data:"):
                        data = line[len("data:") :].strip()
                        payload = self._parse_data(data)
                        if event is not None:
                            payload.update(event)
                            event = None
                        if self.on_event is not None:
                            await self.on_event(payload)

    @staticmethod
    def _parse_data(data: str) -> dict[str, Any]:
        """El bus envía el Envelope como JSON en el campo data."""
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"raw": data}


# Helper para consumo como iterador (tests / uso programático)
async def iter_bus_events(
    agent_id: str,
    bus_url: str = "http://localhost:8420",
    stop: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Itera eventos SSE del bus; termina cuando se setea ``stop`` o se corta la conexión."""
    async with httpx.AsyncClient(base_url=bus_url.rstrip("/"), timeout=None) as client:
        async with client.stream("GET", f"/events/{agent_id}") as resp:
            resp.raise_for_status()
            event: dict[str, Any] | None = None
            async for line in resp.aiter_lines():
                if stop is not None and stop.is_set():
                    return
                if line.startswith("event:"):
                    event = {"event": line[len("event:") :].strip()}
                elif line.startswith("data:"):
                    payload = BusEventClient._parse_data(line[len("data:") :].strip())
                    if event is not None:
                        payload.update(event)
                        event = None
                    yield payload
