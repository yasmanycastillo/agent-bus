from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine


@dataclass
class RunnerResult:
    success: bool
    output: str
    session_id: str | None = None
    exit_code: int = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentRunner:
    """Headless execution runner for autonomous agents (Claude Code, AGY, Aider, Codex, Grok, Mock)."""

    def __init__(
        self,
        agent_id: str,
        provider: str = "claude",
        model: str | None = None,
        worktree_dir: Path | None = None,
        custom_executor: (
            Callable[[str, str | None], Coroutine[Any, Any, RunnerResult]] | None
        ) = None,
    ) -> None:
        self.agent_id = agent_id
        self.provider = provider.lower()
        self.model = model
        self.worktree_dir = worktree_dir or Path.cwd()
        self.custom_executor = custom_executor
        self.session_map: dict[str, str] = {}  # thread_id -> CLI session_id

    def assemble_prompt(
        self,
        task: dict[str, Any] | None = None,
        message: dict[str, Any] | None = None,
        decisions: list[dict[str, Any]] | None = None,
        git_diff_summary: str | None = None,
        extra_instructions: str | None = None,
    ) -> str:
        """Assembles a compact, high-signal prompt with current task and inbox context."""
        parts = [
            f"You are the autonomous worker for agent '{self.agent_id}' in project coordinated via agent-bus.",
            "You must execute the required changes, run tests, and use `agent-bus work` commands if needed.",
        ]

        if task:
            parts.append("\n## Current Assigned Task:")
            parts.append(f"- ID: {task.get('task_id')}")
            parts.append(f"- Title: {task.get('title')}")
            if task.get("description"):
                parts.append(f"- Description: {task.get('description')}")
            if task.get("locked_files"):
                parts.append(f"- Locked files: {', '.join(task.get('locked_files', []))}")

        if message:
            parts.append("\n## High Priority Incoming Message (Reply Needed):")
            parts.append(f"- From: {message.get('from_agent')}")
            parts.append(f"- Type: {message.get('message_type')}")
            body = message.get("body", {})
            if isinstance(body, dict):
                parts.append(f"- Content: {body.get('text', json.dumps(body))}")
            else:
                parts.append(f"- Content: {body}")
            if message.get("related_task"):
                parts.append(f"- Related Task: {message.get('related_task')}")

        if decisions:
            parts.append("\n## Recent Architecture Decisions (ADRs):")
            for d in decisions[-3:]:
                parts.append(f"- [{d.get('decision_id')}]: {d.get('title')} -> {d.get('decision')}")

        if git_diff_summary:
            parts.append(f"\n## Local Git Status / Diff Summary:\n{git_diff_summary.strip()[:1000]}")

        if extra_instructions:
            parts.append(f"\n## Instructions:\n{extra_instructions}")

        parts.append(
            "\n## Operating Rules:\n"
            "1. Lock files before editing with `agent-bus work lock <file>`.\n"
            "2. Run tests to verify your changes.\n"
            "3. If done, unlock files and run `agent-bus work done <task_id>`.\n"
            "4. If you need info from a peer, run `agent-bus work msg <agent> '<question>' --reply-needed`."
        )

        return "\n".join(parts)

    async def execute_turn(
        self,
        prompt: str,
        thread_id: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> RunnerResult:
        """Executes one autonomous agent turn (CLI headless or custom executor)."""
        if self.custom_executor:
            session_id = self.session_map.get(thread_id) if thread_id else None
            result = await self.custom_executor(prompt, session_id)
            if thread_id and result.session_id:
                self.session_map[thread_id] = result.session_id
            return result

        if self.provider == "mock":
            return RunnerResult(
                success=True,
                output=f"[mock:{self.agent_id}] Executed turn successfully.",
                session_id=f"mock-sess-{thread_id or 'default'}",
            )

        if self.provider == "claude":
            return await self._execute_claude_cli(prompt, thread_id, timeout_seconds)

        if self.provider in ("agy", "antigravity"):
            return await self._execute_agy_cli(prompt, thread_id, timeout_seconds)

        if self.provider in ("aider", "codex"):
            return await self._execute_aider_cli(prompt, thread_id, timeout_seconds)

        if self.provider in ("grok", "xai", "openai"):
            return await self._execute_generic_cli(prompt, thread_id, timeout_seconds)

        return RunnerResult(
            success=False,
            output="",
            error=f"Unsupported provider: {self.provider}",
            exit_code=1,
        )

    async def _execute_claude_cli(
        self,
        prompt: str,
        thread_id: str | None,
        timeout_seconds: float,
    ) -> RunnerResult:
        """Invokes Claude Code CLI in non-interactive/headless mode."""
        claude_bin = shutil.which("claude")
        if not claude_bin:
            return RunnerResult(
                success=False,
                output="",
                error="Claude CLI binary ('claude') not found in PATH.",
                exit_code=127,
            )

        cmd = [claude_bin, "-p", prompt, "--output-format", "json"]

        if thread_id and thread_id in self.session_map:
            cmd.extend(["--resume", self.session_map[thread_id]])

        return await self._run_subprocess(cmd, timeout_seconds, thread_id=thread_id)

    async def _execute_agy_cli(
        self,
        prompt: str,
        thread_id: str | None,
        timeout_seconds: float,
    ) -> RunnerResult:
        """Invokes Antigravity / AGY CLI in headless mode."""
        agy_bin = shutil.which("agy") or shutil.which("antigravity")
        if not agy_bin:
            # Fallback to python module or warning
            return RunnerResult(
                success=False,
                output="",
                error="Antigravity CLI binary ('agy' / 'antigravity') not found in PATH.",
                exit_code=127,
            )

        cmd = [agy_bin, "--prompt", prompt]
        if self.model:
            cmd.extend(["--model", self.model])

        return await self._run_subprocess(cmd, timeout_seconds, thread_id=thread_id)

    async def _execute_aider_cli(
        self,
        prompt: str,
        thread_id: str | None,
        timeout_seconds: float,
    ) -> RunnerResult:
        """Invokes Aider CLI (committing inside worktree)."""
        aider_bin = shutil.which("aider")
        if not aider_bin:
            return RunnerResult(
                success=False,
                output="",
                error="Aider CLI binary ('aider') not found in PATH.",
                exit_code=127,
            )

        cmd = [aider_bin, "--message", prompt, "--yes"]
        if self.model:
            cmd.extend(["--model", self.model])

        return await self._run_subprocess(cmd, timeout_seconds, thread_id=thread_id)

    async def _execute_generic_cli(
        self,
        prompt: str,
        thread_id: str | None,
        timeout_seconds: float,
    ) -> RunnerResult:
        """Generic runner fallback for other providers."""
        bin_name = shutil.which(self.provider)
        if not bin_name:
            return RunnerResult(
                success=False,
                output="",
                error=f"CLI binary for '{self.provider}' not found in PATH.",
                exit_code=127,
            )

        cmd = [bin_name, prompt]
        return await self._run_subprocess(cmd, timeout_seconds, thread_id=thread_id)

    async def _run_subprocess(
        self,
        cmd: list[str],
        timeout_seconds: float,
        thread_id: str | None = None,
    ) -> RunnerResult:
        env = os.environ.copy()
        env["AGENT_BUS_AGENT_ID"] = self.agent_id

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.worktree_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return RunnerResult(
                    success=False,
                    output="",
                    error=f"Execution timed out after {timeout_seconds}s",
                    exit_code=-1,
                )

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = process.returncode or 0

            # Attempt to parse session_id if output is JSON
            new_session_id = None
            try:
                data = json.loads(stdout_text)
                if isinstance(data, dict):
                    new_session_id = data.get("session_id")
            except Exception:
                pass

            if thread_id and new_session_id:
                self.session_map[thread_id] = new_session_id

            return RunnerResult(
                success=exit_code == 0,
                output=stdout_text,
                error=stderr_text if exit_code != 0 else None,
                exit_code=exit_code,
                session_id=new_session_id,
            )

        except Exception as exc:
            return RunnerResult(
                success=False,
                output="",
                error=str(exc),
                exit_code=1,
            )
