from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_bus.worker.notifier import ExternalNotifier, NotificationResult


@pytest.mark.asyncio
async def test_notifier_desktop_dispatch(monkeypatch):
    called = []

    async def mock_send_desktop(title, message, level):
        called.append((title, message, level))
        return True

    notifier = ExternalNotifier(enable_desktop=True)
    notifier._send_desktop = mock_send_desktop  # type: ignore

    res = await notifier.notify("Test Alert", "Something happened", level="info")
    assert res.delivered is True
    assert "desktop" in res.channels
    assert len(called) == 1
    assert called[0][0] == "Test Alert"


@pytest.mark.asyncio
async def test_notifier_webhook_dispatch(monkeypatch):
    received_payloads = []

    async def mock_webhook_endpoint(request: Request):
        data = await request.json()
        received_payloads.append(data)
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/webhook", mock_webhook_endpoint, methods=["POST"])])
    transport = ASGITransport(app=app)

    async def mock_send_webhook(url, title, message, level, metadata):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/webhook", json={"title": title, "message": message, "level": level})
            return resp.status_code == 200

    notifier = ExternalNotifier(enable_desktop=False, webhook_urls=["http://test/webhook"])
    notifier._send_webhook = mock_send_webhook  # type: ignore

    res = await notifier.notify("Task Finished", "T1 is done", level="success")
    assert res.delivered is True
    assert len(received_payloads) == 1
    assert received_payloads[0]["title"] == "Task Finished"


@pytest.mark.asyncio
async def test_notifier_helpers():
    notifier = ExternalNotifier(enable_desktop=False)

    async def mock_notify(title, message, level="info", metadata=None):
        return NotificationResult(delivered=True, channels=["mock"])

    notifier.notify = mock_notify  # type: ignore

    res1 = await notifier.notify_goal_completed("Auth system", "Done successfully")
    assert res1.delivered is True

    res2 = await notifier.notify_task_blocked("T10", "Implement DB", "Driver missing")
    assert res2.delivered is True
