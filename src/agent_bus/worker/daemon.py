from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from agent_bus.types import AgentStatus
from agent_bus.worker.client import BusEventClient
from agent_bus.worker.runner import AgentRunner, RunnerResult

logger = logging.getLogger("agent_bus.worker.daemon")


class WorkerDaemon:
    """Autonomous Worker Daemon that continuously listens to agent-bus events (via SSE and polling)
    and drives agent execution without human intervention."""

    def __init__(
        self,
        agent_id: str,
        runner: AgentRunner,
        bus_url: str = "http://localhost:8420",
        poll_interval_seconds: float = 3.0,
        heartbeat_interval_seconds: float = 15.0,
        max_turns_per_task: int = 10,
    ) -> None:
        self.agent_id = agent_id
        self.runner = runner
        self.bus_url = bus_url.rstrip("/")
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.max_turns_per_task = max_turns_per_task
        self._running = False
        self._task_turn_counts: dict[str, int] = {}
        self._client: httpx.AsyncClient | None = None
        self._sse_client: BusEventClient | None = None
        self._wake_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        """Starts the autonomous worker daemon loop."""
        self._running = True
        self._client = httpx.AsyncClient(base_url=self.bus_url, timeout=30.0)

        # Setup SSE event listener for immediate push wakeup
        self._sse_client = BusEventClient(
            agent_id=self.agent_id,
            bus_url=self.bus_url,
            on_event=self._on_sse_event,
        )

        logger.info(f"Starting WorkerDaemon for agent '{self.agent_id}' connected to {self.bus_url}")

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        sse_task = asyncio.create_task(self._sse_client.start())
        event_loop_task = asyncio.create_task(self._event_poll_loop())

        try:
            await asyncio.gather(heartbeat_task, sse_task, event_loop_task)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Gracefully stops the worker daemon."""
        self._running = False
        if self._sse_client:
            self._sse_client.stop()
            self._sse_client = None

        if self._client:
            try:
                await self._client.post(
                    f"/agents/{self.agent_id}/heartbeat",
                )
            except Exception:
                pass
            await self._client.aclose()
            self._client = None
        logger.info(f"WorkerDaemon for agent '{self.agent_id}' stopped.")

    async def _on_sse_event(self, event: dict[str, Any]) -> None:
        """Called when an SSE event arrives from the bus."""
        # High priority wake up for messages requiring reply or task assignments
        if event.get("reply_needed") or event.get("message_type") in ("handoff", "task_assigned"):
            self._wake_event.set()

    async def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                if self._client:
                    await self._client.post(f"/agents/{self.agent_id}/heartbeat")
            except Exception as exc:
                logger.warning(f"Heartbeat failed for '{self.agent_id}': {exc}")
            await asyncio.sleep(self.heartbeat_interval_seconds)

    async def _event_poll_loop(self) -> None:
        while self._running:
            try:
                await self._check_and_process_pending()
            except Exception as exc:
                logger.error(f"Error in worker event loop for '{self.agent_id}': {exc}")

            # Wait either for poll timeout or for an immediate SSE push event
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval_seconds)
                self._wake_event.clear()
            except asyncio.TimeoutError:
                pass

    async def _check_and_process_pending(self) -> None:
        if not self._client:
            return

        # 1. Check pending inbox messages requiring reply (High Priority)
        inbox_resp = await self._client.get(f"/inbox/{self.agent_id}/pending")
        if inbox_resp.status_code == 200:
            pending_data = inbox_resp.json()
            if pending_data.get("reply_needed", 0) > 0:
                messages_resp = await self._client.get(f"/inbox/{self.agent_id}")
                if messages_resp.status_code == 200:
                    messages = messages_resp.json()
                    for msg in messages:
                        if msg.get("reply_needed"):
                            await self._handle_urgent_message(msg)
                            return

        # 2. Check for assigned / in_progress tasks owned by this agent
        tasks_resp = await self._client.get("/tasks", params={"owner": self.agent_id, "status": "in_progress"})
        if tasks_resp.status_code == 200:
            tasks = tasks_resp.json()
            if tasks:
                task = tasks[0]
                await self._handle_active_task(task)
                return

        # 3. Check for free pending tasks to claim
        free_tasks_resp = await self._client.get("/tasks", params={"owner": "free", "status": "pending"})
        if free_tasks_resp.status_code == 200:
            free_tasks = free_tasks_resp.json()
            if free_tasks:
                task_to_claim = free_tasks[0]
                claim_resp = await self._client.post(
                    f"/tasks/{task_to_claim['task_id']}/claim",
                    json={"agent_id": self.agent_id},
                )
                if claim_resp.status_code == 200:
                    logger.info(f"Agent '{self.agent_id}' claimed task {task_to_claim['task_id']}")
                    await self._handle_active_task(claim_resp.json())
                    return

    async def _handle_urgent_message(self, message: dict[str, Any]) -> RunnerResult:
        logger.info(f"Agent '{self.agent_id}' processing urgent message from '{message.get('from_agent')}'")
        await self._set_agent_status(AgentStatus.BUSY, work={"type": "reply", "message_id": message.get("message_id")})

        decisions = await self._fetch_recent_decisions()
        thread_id = message.get("correlation_id") or message.get("message_id")

        prompt = self.runner.assemble_prompt(
            message=message,
            decisions=decisions,
            extra_instructions="Respond directly to the sender's question using `agent-bus work msg`.",
        )

        result = await self.runner.execute_turn(prompt, thread_id=thread_id)

        # Archive processed message
        if self._client and message.get("message_id"):
            await self._client.post(f"/inbox/{self.agent_id}/{message['message_id']}/archive")

        await self._set_agent_status(AgentStatus.ONLINE, work=None)
        return result

    async def _handle_active_task(self, task: dict[str, Any]) -> RunnerResult:
        task_id = task.get("task_id", "")
        turns = self._task_turn_counts.get(task_id, 0) + 1
        self._task_turn_counts[task_id] = turns

        if turns > self.max_turns_per_task:
            logger.warning(f"Task {task_id} exceeded max turns ({self.max_turns_per_task}). Pausing task.")
            # Do not loop infinitely on stuck tasks
            return RunnerResult(success=False, output="", error=f"Task {task_id} exceeded max turn limit.")

        logger.info(f"Agent '{self.agent_id}' executing task {task_id} (turn {turns}/{self.max_turns_per_task})")
        await self._set_agent_status(AgentStatus.BUSY, work={"type": "task", "task_id": task_id})

        decisions = await self._fetch_recent_decisions()
        prompt = self.runner.assemble_prompt(task=task, decisions=decisions)

        result = await self.runner.execute_turn(prompt)

        await self._set_agent_status(AgentStatus.ONLINE, work=None)
        return result

    async def _fetch_recent_decisions(self) -> list[dict[str, Any]]:
        if not self._client:
            return []
        try:
            resp = await self._client.get("/decisions")
            return resp.json() if resp.status_code == 200 else []
        except Exception:
            return []

    async def _set_agent_status(self, status: AgentStatus, work: dict[str, Any] | None) -> None:
        if not self._client:
            return
        try:
            await self._client.post(
                f"/agents/{self.agent_id}/active-work",
                json={"work": work},
            )
        except Exception:
            pass
