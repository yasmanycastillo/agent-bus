"""Opt-in Hermes Agent acceptance; real model calls, isolated hub and credentials.

Run with the repository Python. Private artifacts stay in a mode-0700 temporary
directory; only evidence.json is suitable for publication. Never run in pytest.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

import httpx
import yaml

from acceptance_real_clients import Acceptance
from agent_bus.orchestrator.hermes import HermesOrchestrator
from agent_bus.orchestrator.schema import parse_and_validate_plan


def verify(output: Path):
    """Audit saved bus state, Hermes tool calls and HTTP traces without inference."""
    with sqlite3.connect(f"file:{output}/runtime/data/bus.db?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        deliveries = [dict(r) for r in db.execute("SELECT * FROM inbox ORDER BY timestamp")]
        tasks = [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY task_id")]
    assert len(deliveries) == 4 and all(r["archived"] for r in deliveries)
    sent = [r for r in deliveries if r["from_agent"] == "codex"]
    replies = [r for r in deliveries if r["from_agent"] == "hermes"]
    assert len(sent) == len(replies) == 2
    assert all(r["correlation_id"] == s["message_id"] for r, s in zip(replies, sent))
    plan = parse_and_validate_plan(json.loads(replies[0]["body"])["text"], operation_key="hermes-plan")
    assert len(plan.tasks) == len(tasks) == 2
    assert {t["status"] for t in tasks} == {"pending", "blocked"}
    assert all(t["operation_key"] == "hermes-plan" for t in tasks)
    assert plan.tasks[1].depends_on == [plan.tasks[0].task_id]
    stored = {t["task_id"]: t for t in tasks}
    for item in plan.tasks:
        assert json.loads(stored[item.task_id]["depends_on"]) == item.depends_on
    marker = re.search(r"Remember marker (\w+)", json.loads(sent[0]["body"])["text"]).group(1)
    assert marker in json.loads(replies[1]["body"])["text"]
    assert marker not in json.loads(sent[1]["body"])["text"]
    turns = []
    for n in (1, 2):
        trace = (output / f"turn-{n}.log").read_text()
        turns.append({"session_id": re.findall(r"session_id:\s*([\w-]+)", trace)[-1],
                      "sha256": hashlib.sha256(trace.encode()).hexdigest()})
    assert turns[0]["session_id"] == turns[1]["session_id"]
    with sqlite3.connect(f"file:{output}/hermes/state.db?mode=ro", uri=True) as db:
        model = db.execute("SELECT model FROM sessions WHERE id=?", (turns[0]["session_id"],)).fetchone()[0]
        calls = []
        for (raw,) in db.execute("SELECT tool_calls FROM messages WHERE tool_calls IS NOT NULL"):
            calls.extend(json.loads(raw))
    resolved = []
    for call in calls:
        function = call["function"]
        arguments = json.loads(function["arguments"])
        if function["name"] == "tool_describe":
            assert all(name.startswith("mcp__agent_bus__") for name in arguments["names"])
            continue
        if function["name"] == "tool_call":
            resolved.append({"name": arguments["name"], "arguments": arguments["arguments"]})
        else:
            resolved.append({"name": function["name"], "arguments": arguments})
    names = [c["name"] for c in resolved]
    assert names and all("agent" in name and name.endswith(("read_messages", "reply_message")) for name in names), names
    reply_calls = [c["arguments"] for c in resolved if c["name"].endswith("reply_message")]
    assert len(reply_calls) == 3 and reply_calls[0] == reply_calls[1]
    log = (output / "hub.log").read_text()
    assert log.count('"POST /tasks/batch HTTP/1.1" 200') == 2
    metadata = json.loads((output / "run.json").read_text())
    evidence = {"client": metadata["client"], "model": model, "turns": turns,
                "deliveries": 4, "pending": 0, "tasks": 2,
                "statuses": sorted(t["status"] for t in tasks), "identical_reply_replay": True,
                "batch_requests": 2, "resume_same_session": True, "remembered_marker": True,
                "tool_names": sorted(set(names)),
                "publication": "validated Python bridge to POST /tasks/batch"}
    (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    print("Private artifacts:", output)
    return evidence


def main():
    output = Path(tempfile.mkdtemp(prefix="agent-bus-hermes-"))
    (output / "run.json").write_text(json.dumps({
        "client": subprocess.check_output(["hermes", "--version"], text=True).splitlines()[0],
    }))
    bus = Acceptance(output)
    try:
        bus.prepare()
        subprocess.run(
            [sys.executable, "-c", "from agent_bus.cli.main import app; app()",
             "auth", "create", "--agent", "hermes"],
            env=bus.env, cwd=bus.project, check=True, capture_output=True,
        )
        home = output / "hermes"
        home.mkdir(mode=0o700)
        original = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        # Copy provider authentication privately; never print or publish it.
        for name in (".env", "auth.json"):
            if (original / name).exists():
                shutil.copyfile(original / name, home / name)
                (home / name).chmod(0o600)
        config = yaml.safe_load((original / "config.yaml").read_text()) or {}
        config["mcp_servers"] = {"agent-bus": {
            "command": sys.executable,
            "args": ["-c", "from agent_bus.cli.main import app; app()", "mcp-server"],
            "env": {**{k: v for k, v in bus.env.items()
                       if k.startswith("AGENT_BUS_") or k == "PYTHONPATH"},
                    "AGENT_BUS_AGENT_ID": "hermes",
                    "AGENT_BUS_SESSION_FILE": str(bus.runtime / "credentials/hermes.json")},
            "enabled": True,
        }}
        config.pop("hooks", None)
        (home / "config.yaml").write_text(yaml.safe_dump(config))
        (home / "config.yaml").chmod(0o600)
        child_env = {**bus.env, "HERMES_HOME": str(home)}
        # Do not inherit a profile selection that redirects the isolated home.
        child_env.pop("HERMES_PROFILE", None)
        turns = []

        def run(prompt, resume=None):
            cmd = ["hermes", "chat", "--oneshot", "-Q", "--ignore-rules",
                   "--max-turns", "12", "--run-budget", "150", "-q", prompt]
            if resume:
                cmd += ["--resume", resume, "--no-restore-cwd"]
            result = subprocess.run(cmd, cwd=bus.project, env=child_env,
                                    capture_output=True, text=True, timeout=210)
            trace = result.stdout + "\n" + result.stderr
            (output / f"turn-{len(turns)+1}.log").write_text(trace)
            if result.returncode:
                raise RuntimeError(f"Hermes failed; private trace: {output}")
            sessions = re.findall(r"session_id:\s*([\w-]+)", trace)
            if not sessions:
                raise RuntimeError(f"Hermes returned no session ID; private trace: {output}")
            turns.append({"session_id": sessions[-1], "exit_code": result.returncode,
                          "sha256": hashlib.sha256(trace.encode()).hexdigest()})
            return sessions[-1]

        with httpx.Client(base_url=bus.url, trust_env=False, headers={
            "Authorization": f"Bearer {bus.clients['codex']['token']}",
            "X-Agent-Bus-Project": "t13-acceptance",
        }) as client:
            def send(text, key):
                response = client.post("/messages", json={
                    "from_agent": "codex", "to_agent": "hermes",
                    "message_type": "inbox", "body": {"text": text},
                    "reply_needed": True, "idempotency_key": key,
                })
                response.raise_for_status()

            marker = uuid.uuid4().hex
            send(
                "Acceptance only; no coding or filesystem tools. Remember marker " + marker +
                ". Reply with a raw JSON task plan for a Python slugify function, exactly two tasks: "
                "implement-slug then test-slug depending on implement-slug. Top-level keys: "
                "objective, summary, tasks. Each task: task_id, title, description, "
                "acceptance_criteria (string array), test_cmd (string array), depends_on (ID array). "
                "Use reply_message with acknowledge=true and idempotency_key=hermes-plan. "
                "Repeat that SAME reply call once with identical arguments to test deduplication.",
                "objective",
            )
            sid = run("Use only agent-bus MCP. Read your pending messages, follow the acceptance "
                      "request, reply through MCP and acknowledge it. Do not use shell/files/tools "
                      "outside agent-bus. End with a brief status.")
            response = client.get("/inbox/codex/messages")
            response.raise_for_status()
            messages = response.json()["messages"]
            assert len(messages) == 1, "Expected one deduplicated plan reply"
            plan = parse_and_validate_plan(messages[0]["body"]["text"], operation_key="hermes-plan")
            assert len(plan.tasks) == 2 and plan.tasks[1].depends_on == [plan.tasks[0].task_id]
            # Explicit bridge: MCP has no task creation tool. Validate the real
            # agent's plan, then use the existing authenticated batch publisher.
            publisher = HermesOrchestrator()
            first = asyncio.run(publisher.publish_breakdown(plan, client))
            second = asyncio.run(publisher.publish_breakdown(plan, client))
            assert first == second and len(first) == 2, "Batch replay changed tasks"
            tasks = client.get("/tasks").json()
            assert len(tasks) == 2
            assert {t["status"] for t in tasks} == {"pending", "blocked"}
            ack = client.post("/inbox/codex/ack", json={"message_ids": [messages[0]["message_id"]]})
            ack.raise_for_status()
            # Hermes is disconnected when this second delivery is persisted.
            send("Reply with the remembered marker only, using reply_message acknowledge=true "
                 "and idempotency_key=hermes-resumed. Do not use filesystem tools.", "resume")
            resumed = run("Continue the acceptance: read your pending agent-bus messages and "
                          "reply and acknowledge through MCP only.", resume=sid)
            assert resumed == sid, "Resume changed session"
            page = client.get("/inbox/codex/messages").json()["messages"]
            assert len(page) == 1 and marker in page[0]["body"]["text"], "Context not recovered"
            client.post("/inbox/codex/ack", json={"message_ids": [page[0]["message_id"]]}).raise_for_status()
        with sqlite3.connect(bus.runtime / "data/bus.db") as db:
            deliveries = db.execute("SELECT count(*) FROM inbox").fetchone()[0]
            pending = db.execute("SELECT count(*) FROM inbox WHERE archived = 0").fetchone()[0]
        assert deliveries == 4 and pending == 0
    finally:
        bus.close()
    verify(output)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--verify":
        verify(Path(sys.argv[2]).resolve())
    else:
        main()
