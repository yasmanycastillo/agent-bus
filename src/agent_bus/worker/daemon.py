from __future__ import annotations

import asyncio
import logging
import hashlib
import time
from pathlib import Path
from typing import Any

import httpx

from agent_bus.config import get_bus_url
from agent_bus.security import async_bus_client
from agent_bus.worker.execution import ExecutionGuard

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
        bus_url: str | None = None,
        poll_interval_seconds: float = 3.0,
        heartbeat_interval_seconds: float = 15.0,
        max_turns_per_task: int = 10,
        max_message_attempts: int = 5,
    ) -> None:
        if runner.agent_id != agent_id:
            raise ValueError("Runner identity must match its daemon")
        self.agent_id = agent_id
        self.runner = runner
        self.bus_url = get_bus_url(bus_url)
        self.runner.bus_url = self.bus_url
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.max_turns_per_task = max_turns_per_task
        self.max_message_attempts = max(1, max_message_attempts)
        self._running = False
        self._task_turn_counts: dict[str, int] = {}
        self._client: httpx.AsyncClient | None = None
        self._sse_client: BusEventClient | None = None
        self._wake_event: asyncio.Event = asyncio.Event()
        self._message_cursor: str | None = None
        self._message_retry_after: dict[str, float] = {}

    async def start(self) -> None:
        """A worker and watcher cannot automatically execute the same participant."""
        with ExecutionGuard(self.agent_id, kind="worker"):
            await self._run()

    async def _run(self) -> None:
        """Starts the autonomous worker daemon loop after acquiring exclusion."""
        self._client = async_bus_client(self.agent_id, base_url=self.bus_url, timeout=30.0)
        try:
            response = await self._client.post("/register", json={
                "agent_id": self.agent_id, "display_name": self.agent_id,
            })
            if response.status_code != 409:
                response.raise_for_status()
        except Exception:
            await self._client.aclose()
            self._client = None
            raise
        self._running = True

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
        if event.get("event") == "reset" or event.get("reply_needed") or event.get("message_type") in ("handoff", "task_assigned"):
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

        self._message_retry_after = {
            key: deadline for key, deadline in self._message_retry_after.items()
            if deadline > time.monotonic()
        }
        # A bounded page of persisted deliveries survives disconnects and restarts.
        params = {"limit": 10, "reply_needed": "true"}
        if self._message_cursor:
            params["cursor"] = self._message_cursor
        messages_resp = await self._client.get(f"/inbox/{self.agent_id}/messages", params=params)
        messages_resp.raise_for_status()
        page = messages_resp.json()
        self._message_cursor = page.get("next_cursor")
        processed = False
        for message in page["messages"]:
            message_id = message["message_id"]
            if time.monotonic() < self._message_retry_after.get(message_id, 0):
                continue
            await self._handle_urgent_message(message)
            processed = True
        if processed:
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

    async def _record_message_failure(self, message_id: str, error: str) -> None:
        self._message_retry_after[message_id] = time.monotonic() + max(3.0, self.poll_interval_seconds)
        if not self._client:
            return
        try:
            response = await self._client.post(
                f"/inbox/{self.agent_id}/{message_id}/fail", json={"error": error.strip()[:500] or "Runner failed without error details"},
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Could not record message failure: %s", exc)

    async def _handle_urgent_message(self, message: dict[str, Any]) -> RunnerResult:
        message_id = message["message_id"]
        if not self._client:
            return RunnerResult(success=False, output="", error="Bus client unavailable")
        try:
            state = await self._client.get(f"/inbox/{self.agent_id}/{message_id}")
            state.raise_for_status()
            message = state.json()
            if message.get("acknowledged"):
                self._message_retry_after.pop(message_id, None)
                return RunnerResult(success=True, output="", metadata={"already_acknowledged": True})
            attempts = int(message.get("attempts", 0) or 0)
            if attempts >= self.max_message_attempts:
                detail = (
                    f"Message {message_id} blocked after {attempts} failed attempts "
                    f"(limit {self.max_message_attempts})"
                )
                logger.error(detail)
                await self._set_agent_status(AgentStatus.AWAY, work={
                    "type": "blocked_message", "message_id": message_id, "error": detail,
                })
                return RunnerResult(False, "", error=detail, metadata={"blocked": True, "attempts": attempts})
            await self._set_agent_status(AgentStatus.BUSY, work={"type": "reply", "message_id": message_id})
            prompt = self.runner.assemble_prompt(
                message=message,
                decisions=await self._fetch_recent_decisions(),
                extra_instructions=(
                    "Return your reply as the final response text. The worker will send it "
                    "and acknowledge this message. Do not send or acknowledge it yourself via CLI or MCP."
                ),
            )
            thread_id = message.get("conversation_id") or message.get("correlation_id") or message_id
            result = await self.runner.execute_turn(prompt, thread_id=thread_id)
            if not result.success:
                await self._record_message_failure(message_id, result.error or "Runner failed")
                return result
            if not result.output.strip():
                raise ValueError("Runner returned no reply text")
            key = "worker-reply:" + message_id
            if len(key) > 128:
                key = "worker-reply:" + hashlib.sha256(message_id.encode()).hexdigest()
            response = await self._client.post(
                f"/inbox/{self.agent_id}/{message_id}/reply",
                json={"body": {"text": result.output}, "idempotency_key": key,
                      "reply_needed": False, "acknowledge": True},
            )
            response.raise_for_status()
            self._message_retry_after.pop(message_id, None)
            return result
        except asyncio.CancelledError:
            await asyncio.shield(self._record_message_failure(message_id, "Runner cancelled"))
            raise
        except Exception as exc:
            await self._record_message_failure(message_id, str(exc))
            return RunnerResult(success=False, output="", error=str(exc))
        finally:
            await self._set_agent_status(AgentStatus.ONLINE, work=None)

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

        if result.success:
            await self._commit_and_submit_review(task_id)

        await self._set_agent_status(AgentStatus.ONLINE, work=None)
        return result

    async def _commit_and_submit_review(self, task_id: str) -> None:
        """Commit the worker checkout and enqueue it for serialized integration."""
        checkout = self.runner.worktree_dir.resolve()
        # Test/custom runners may execute from the coordinator checkout. Never
        # stage that shared tree implicitly.
        if checkout == Path.cwd().resolve() or not (checkout / ".git").exists():
            return
        try:
            status = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain", cwd=str(checkout),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await status.communicate()
            if not stdout.strip():
                logger.warning("Task %s completed without committed changes", task_id)
                return
            add = await asyncio.create_subprocess_exec("git", "add", "-A", cwd=str(checkout))
            if await add.wait() != 0:
                raise RuntimeError("git add failed")
            commit = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", f"feat(agent): complete {task_id}", cwd=str(checkout),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await commit.communicate()
            if commit.returncode != 0:
                raise RuntimeError((err or out).decode(errors="replace")[-500:])
            if self._client:
                response = await self._client.post(f"/tasks/{task_id}/review")
                response.raise_for_status()
        except Exception as exc:
            logger.error("Could not submit task %s for review: %s", task_id, exc)

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
