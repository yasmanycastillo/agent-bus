"""Disposable Phase -1 probe. Not a runtime adapter."""

from __future__ import annotations

import os
import signal
import subprocess
import uuid
from dataclasses import dataclass

import httpx


class AttemptError(RuntimeError):
    """The probe refused to start or settle an attempt."""


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    task_id: str
    state: str
    candidate_sha: str | None = None
    note: str = ""


class ExternalCommandProbe:
    """Run a command whose task state stays in Agent Bus messages."""

    def __init__(self, client: httpx.Client, agent_id: str = "probe") -> None:
        self.client = client
        self.agent_id = agent_id
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def launch(self, task_id: str, command: list[str], cwd: str, timeout: float) -> Attempt:
        started = self.begin(task_id, command, cwd)
        return self.finish_in(started.attempt_id, cwd, timeout)

    def begin(self, task_id: str, command: list[str], cwd: str) -> Attempt:
        self._refuse_if_busy(task_id)
        attempt = Attempt(f"att-{uuid.uuid4().hex[:12]}", task_id, "launched")
        self._store(attempt)
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_BUS")}
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._processes[attempt.attempt_id] = process
        return attempt

    def finish_in(self, attempt_id: str, cwd: str, timeout: float) -> Attempt:
        current = self._require(attempt_id)
        process = self._processes.get(attempt_id)
        if process is None:
            raise AttemptError(f"Attempt {attempt_id} has no process in this probe")
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(process)
            unknown = Attempt(attempt_id, current.task_id, "unknown", note="timeout")
            self._store(unknown)
            return unknown
        if process.returncode != 0:
            failed = Attempt(attempt_id, current.task_id, "cancelled", note=f"exit {process.returncode}")
            self._store(failed)
            return failed
        completed = Attempt(attempt_id, current.task_id, "completed", candidate_sha=self._head(cwd))
        self._store(completed)
        return completed

    def cancel(self, attempt_id: str) -> Attempt:
        current = self._require(attempt_id)
        if current.state in {"completed", "cancelled"}:
            return current
        process = self._processes.get(attempt_id)
        if process is not None and process.poll() is None:
            self._kill(process)
        cancelled = Attempt(attempt_id, current.task_id, "cancelled", note="operator cancel")
        self._store(cancelled)
        return cancelled

    def reconcile(self, attempt_id: str) -> Attempt:
        current = self._require(attempt_id)
        if current.state != "unknown" and current.state != "launched":
            return current
        process = self._processes.get(attempt_id)
        if process is not None and process.poll() is None:
            raise AttemptError(f"Attempt {attempt_id} is still running")
        cancelled = Attempt(attempt_id, current.task_id, "cancelled", note="reconciled")
        self._store(cancelled)
        return cancelled

    def latest(self, task_id: str) -> list[Attempt]:
        grouped: dict[str, Attempt] = {}
        for attempt in self._history(task_id):
            grouped[attempt.attempt_id] = attempt
        return list(grouped.values())

    def _refuse_if_busy(self, task_id: str) -> None:
        for attempt in self.latest(task_id):
            if attempt.state in {"launched", "unknown"}:
                raise AttemptError(f"Attempt {attempt.attempt_id} must be reconciled before another launch")
            if attempt.state == "completed":
                raise AttemptError(f"Task {task_id} already has completed attempt {attempt.attempt_id}")

    def _require(self, attempt_id: str) -> Attempt:
        for task_attempt in self._history_all():
            if task_attempt.attempt_id == attempt_id:
                found = task_attempt
                break
        else:
            raise AttemptError(f"Unknown attempt {attempt_id}")
        for attempt in self._history(found.task_id):
            if attempt.attempt_id == attempt_id:
                found = attempt
        return found

    def _history_all(self) -> list[Attempt]:
        response = self.client.get(f"/inbox/{self.agent_id}")
        response.raise_for_status()
        attempts = []
        for message in response.json():
            body = message.get("body") or {}
            if body.get("topic") != "external_attempt":
                continue
            context = body.get("context") or {}
            attempts.append(Attempt(
                context["attempt_id"], context["task_id"], context["state"],
                context.get("candidate_sha"), context.get("note") or "",
            ))
        return attempts

    def _history(self, task_id: str) -> list[Attempt]:
        return [attempt for attempt in self._history_all() if attempt.task_id == task_id]

    def _store(self, attempt: Attempt) -> None:
        response = self.client.post("/messages", json={
            "from_agent": self.agent_id,
            "to_agent": self.agent_id,
            "message_type": "inbox",
            "related_task": attempt.task_id,
            "body": {
                "topic": "external_attempt",
                "context": {
                    "attempt_id": attempt.attempt_id,
                    "task_id": attempt.task_id,
                    "state": attempt.state,
                    "candidate_sha": attempt.candidate_sha,
                    "note": attempt.note,
                },
            },
        })
        response.raise_for_status()

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)

    @staticmethod
    def _head(cwd: str) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()
