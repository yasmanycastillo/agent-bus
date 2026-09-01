"""Tests del cliente SSE del worker (T1)."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_bus.worker.client import BusEventClient, iter_bus_events


def _sse_lines(*payloads: dict) -> list[str]:
    lines = []
    for p in payloads:
        lines.append("event: message")
        lines.append(f"data: {json.dumps(p)}")
    return lines


@pytest.mark.asyncio
async def test_parse_data_dict():
    parsed = BusEventClient._parse_data('{"from_agent": "claude"}')
    assert parsed["from_agent"] == "claude"


@pytest.mark.asyncio
async def test_parse_data_invalid_json_returns_raw():
    parsed = BusEventClient._parse_data("not-json")
    assert parsed == {"raw": "not-json"}


@pytest.mark.asyncio
async def test_consume_sse_dispatches_events(monkeypatch):
    received: list[dict] = []

    async def on_event(payload: dict) -> None:
        received.append(payload)
        if len(received) >= 2:
            client.stop()

    client = BusEventClient("claude", on_event=on_event)
    lines = iter(
        _sse_lines(
            {"from_agent": "agy", "body": {"text": "hola"}},
            {"from_agent": "agy", "body": {"text": "chao"}},
        )
    )

    class FakeStream:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for line in lines:
                yield line

    class FakeResp:
        async def __aenter__(self):
            return FakeStream()

        async def __aexit__(self, *a):
            return False

    class FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method: str, url: str):
            return FakeResp()

    import agent_bus.worker.client as client_mod

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", FakeAsyncClient)

    await asyncio.wait_for(client.start(), timeout=5)
    assert len(received) == 2
    assert received[0]["from_agent"] == "agy"
    assert received[0]["event"] == "message"


@pytest.mark.asyncio
async def test_reconnect_backoff_resets_on_clean_session():
    client = BusEventClient("claude", reconnect_initial_delay=0.01, reconnect_max_delay=0.05)
    # Simula dos sesiones limpias seguidas: el delay debe resetear al inicial
    sessions = 0

    async def fake_consume():
        nonlocal sessions
        sessions += 1
        if sessions >= 2:
            client.stop()

    client._consume_sse = fake_consume  # type: ignore[method-assign]
    await asyncio.wait_for(client.start(), timeout=5)
    assert sessions == 2


@pytest.mark.asyncio
async def test_start_retries_after_error():
    client = BusEventClient("claude", reconnect_initial_delay=0.01)
    calls = 0

    async def fake_consume():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("boom")
        client.stop()

    client._consume_sse = fake_consume  # type: ignore[method-assign]
    await asyncio.wait_for(client.start(), timeout=5)
    assert calls == 2  # sobrevivió al error y reconectó
