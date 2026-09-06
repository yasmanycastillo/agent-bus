"""Opt-in acceptance using installed, authenticated Claude Code and Codex CLIs.

Run with the prepared development interpreter. Uses real model calls; never part
of pytest. Private traces and credentials live in a new temporary directory.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

import httpx


class Acceptance:
    def __init__(self, output: Path):
        self.output = output.resolve()
        self.project = self.output / "project"
        self.project.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.runtime = self.output / "runtime"
        self.source = Path(__file__).resolve().parents[1] / "src"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BUS_")}
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE"):
            self.env.pop(key, None)
        self.env.update(
            PYTHONPATH=str(self.source), AGENT_BUS_CONFIG_DIR=str(self.runtime),
            AGENT_BUS_PROJECT_ROOT=str(self.project), AGENT_BUS_PROJECT_ID="t13-acceptance",
            AGENT_BUS_DATABASE_PATH=str(self.runtime / "data" / "bus.db"), AGENT_BUS_ALLOW_UNSIGNED="0",
        )
        self.tokens = []
        self.turns = []
        self.clients = {}
        self.model = "gpt-6-astra"
        config = Path.home() / ".codex" / "config.toml"
        if config.exists():
            self.model = tomllib.loads(config.read_text()).get("model", self.model)

    def prepare(self):
        for actor in ("claude", "codex", "human"):
            subprocess.run(
                [sys.executable, "-c", "from agent_bus.cli.main import app; app()", "auth", "create",
                 "--agent", actor, "--role", "admin" if actor == "human" else "agent"],
                cwd=self.project, env=self.env, capture_output=True, text=True, check=True,
            )
            session = json.loads((self.runtime / "credentials" / f"{actor}.json").read_text())
            self.tokens.append(session["token"])
            self.clients[actor] = session
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.env["AGENT_BUS_URL"] = self.url
        self.log = (self.output / "hub.log").open("w")
        self.hub = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent_bus.core.bus:create_app", "--factory",
             "--fd", str(self.listener.fileno()), "--log-level", "info"],
            env=self.env, cwd=self.project, stdout=self.log, stderr=self.log,
            pass_fds=(self.listener.fileno(),), start_new_session=True,
        )
        self.listener.close()
        self.http = httpx.Client(base_url=self.url, headers={
            "Authorization": f"Bearer {self.clients['human']['token']}",
            "X-Agent-Bus-Project": "t13-acceptance",
        }, trust_env=False, timeout=10)
        deadline = time.monotonic() + 10
        while True:
            try:
                self.http.get("/health").raise_for_status()
                break
            except httpx.TransportError:
                if time.monotonic() > deadline or self.hub.poll() is not None:
                    raise RuntimeError("Temporary hub did not start")
                time.sleep(.05)

    def redact(self, text):
        for token in self.tokens:
            text = text.replace(token, "<redacted>")
        return text

    def command(self, actor, prompt, resume=None):
        child_env = dict(self.env, AGENT_BUS_AGENT_ID=actor,
                         AGENT_BUS_SESSION_FILE=str(self.runtime / "credentials" / f"{actor}.json"))
        mcp_env = {k: v for k, v in child_env.items() if k.startswith("AGENT_BUS_") or k == "PYTHONPATH"}
        mcp_args = ["-c", "from agent_bus.cli.main import app; app()", "mcp-server"]
        if actor == "claude":
            config = {"mcpServers": {"agent_bus": {"command": sys.executable, "args": mcp_args, "env": mcp_env}}}
            command = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                       "--strict-mcp-config", "--mcp-config", json.dumps(config),
                       "--setting-sources", "", "--tools", "", "--allowedTools", "mcp__agent_bus__*",
                       "--permission-mode", "dontAsk", "--disable-slash-commands",
                       "--system-prompt", "You are a bounded MCP acceptance client. Use only agent_bus MCP tools. Follow the test steps precisely. Do not use shell, files, or other servers."]
            if resume:
                command += ["--resume", resume]
            return command + [prompt], child_env
        command = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--json",
                   "--model", self.model, "-s", "read-only", "-c", 'approval_policy="never"',
                   "-c", 'model_reasoning_effort="low"']
        settings = {"command": sys.executable, "args": mcp_args, "env": mcp_env,
                    "required": True, "startup_timeout_sec": 20, "tool_timeout_sec": 150,
                    "default_tools_approval_mode": "approve"}
        for key, value in settings.items():
            literal = "{" + ",".join(f"{k}={json.dumps(v)}" for k, v in value.items()) + "}" if isinstance(value, dict) else json.dumps(value)
            command += ["-c", f"mcp_servers.agent_bus.{key}={literal}"]
        if resume:
            command += ["resume", resume]
        return command + [prompt], child_env

    async def turn(self, actor, phase, prompt, resume=None, timeout=240):
        command, env = self.command(actor, prompt, resume)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(*command, cwd=self.project, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = await process.communicate()
        stdout, stderr = self.redact(stdout.decode(errors="replace")), self.redact(stderr.decode(errors="replace"))
        (self.output / f"{phase}-{actor}.jsonl").write_text(stdout)
        (self.output / f"{phase}-{actor}.stderr").write_text(stderr)
        events = []
        for line in stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        session = next((e.get("thread_id") or e.get("session_id") for e in events if e.get("thread_id") or e.get("session_id")), None)
        calls = []
        for event in events:
            item = event.get("item", {})
            if item.get("type") == "mcp_tool_call" and event.get("type") == "item.completed":
                calls.append({"tool": item.get("tool"), "status": item.get("status"), "arguments": item.get("arguments"), "result": item.get("result")})
            for item in (event.get("message", {}).get("content", []) or []):
                if item.get("type") == "tool_use":
                    calls.append({"tool": item.get("name"), "arguments": item.get("input")})
        record = {"actor": actor, "phase": phase, "exit_code": process.returncode,
                  "timed_out": timed_out, "seconds": round(time.monotonic() - started, 2),
                  "session_id": session, "calls": calls}
        record["model"] = next((e.get("model") for e in events if e.get("model")), self.model if actor == "codex" else None)
        record["client_error"] = any(e.get("is_error") is True or e.get("type") == "turn.failed" for e in events)
        self.turns.append(record)
        (self.output / "turns.json").write_text(json.dumps(self.turns, indent=2))
        print(json.dumps({k: v for k, v in record.items() if k != "calls"} | {"tools": [c["tool"] for c in calls]}), flush=True)
        if not calls:
            print(self.redact((stderr + stdout)[-1800:]), flush=True)
        return record

    def snapshot(self):
        with sqlite3.connect(self.env["AGENT_BUS_DATABASE_PATH"]) as db:
            db.row_factory = sqlite3.Row
            return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
                    for table in ("inbox", "inbox_delivery_state", "message_idempotency", "tasks", "locks")}

    async def scenario(self):
        self.http.post('/tasks', json={'task_id': 'T13-RACE', 'title': 'Acceptance contention'}).raise_for_status()
        challenge = 'challenge-' + uuid.uuid4().hex[:12]
        markers = {actor: actor + '-memory-' + uuid.uuid4().hex[:12] for actor in ('claude', 'codex')}
        race = (
            'Use only agent_bus MCP tools. First claim_task T13-RACE once; a conflict is expected for one participant. '
            'Then acquire_lock file_path="shared.txt", scope="project", ttl_seconds=600 once. '
            'Do not release the lock or complete the task in this turn. Continue even if either conflicts. '
        )
        claude = asyncio.create_task(self.turn('claude', 'exchange', race +
            f'Remember this private conversation marker for your next resumed turn: {markers["claude"]}. '
            'Now call wait_for_updates(timeout=120) before read_messages. If no message yet, wait once more. '
            'Read the message sent by codex. Reply using reply_message with text equal to "ACK " plus the exact text received, '
            'idempotency_key="claude-reply", acknowledge=true. Repeat EXACTLY that reply_message with the same arguments and key once '
            'to test idempotency. Do not send another message or output the private marker; then stop.'))
        # Delay the sender until the receiver is actually subscribed, rather than
        # merely presenting the clients with pre-populated inboxes.
        deadline = time.monotonic() + 75
        waiting_observed = False
        while time.monotonic() < deadline and not claude.done():
            if 'GET /inbox/claude/events HTTP/' in (self.output / 'hub.log').read_text():
                waiting_observed = True
                break
            await asyncio.sleep(.2)
        codex = asyncio.create_task(self.turn('codex', 'exchange', race +
            f'Remember this private conversation marker for your next resumed turn: {markers["codex"]}. '
            f'Use post_message to claude, text="{challenge}", idempotency_key="codex-challenge", reply_needed=true. '
            'Repeat post_message with EXACTLY the same arguments/key once to test idempotency. '
            'Do not read or wait for a reply in this turn. Then stop.'))
        first_claude, first_codex = await asyncio.gather(claude, codex)
        for record in (first_claude, first_codex):
            if record['exit_code'] or record['timed_out'] or record['client_error'] or not record['session_id']:
                raise RuntimeError(f"Client failed during exchange: {record['actor']}")
        before_resume = self.snapshot()
        resumed_codex = await self.turn('codex', 'resume',
            'Use only agent_bus MCP tools. This is the same conversation, resumed after disconnect. '
            'Read your pending inbox. Validate that the reply contains the challenge you sent in your earlier turn. '
            'ACK the reply with ack_messages. Then post_message to claude with text equal to the private conversation '
            'marker you were told to remember in your previous turn, idempotency_key="codex-resumed", reply_needed=true. '
            'If you acquired shared.txt previously, release it using the original acquisition_id and scope project. '
            'If you own T13-RACE, complete it. Do not wait or read again; stop.', resume=first_codex['session_id'])
        before_claude_resume = self.snapshot()
        resumed_claude = await self.turn('claude', 'resume',
            'Use only agent_bus MCP tools. This is the same conversation, resumed after disconnect. Read pending messages. '
            'Reply to the new codex message using reply_message, with text equal to your own private conversation marker '
            'from your first turn, idempotency_key="claude-resumed", acknowledge=true. '
            'If you acquired shared.txt previously, release it using the original acquisition_id and scope project. '
            'If you own T13-RACE, complete it. Then stop.', resume=first_claude['session_id'])
        final = self.snapshot()
        (self.output / 'snapshots.json').write_text(json.dumps({
            'before_codex_resume': before_resume, 'before_claude_resume': before_claude_resume,
            'final': final,
        }, indent=2))
        inbox = final['inbox']
        bodies = [json.loads(row['body'])['text'] for row in inbox]
        keys = [row['idempotency_key'] for row in final['message_idempotency']]
        tool_counts = {}
        for turn in self.turns:
            counts = tool_counts.setdefault(turn['actor'], {})
            for call in turn['calls']:
                name = call['tool'].removeprefix('mcp__agent_bus__')
                counts[name] = counts.get(name, 0) + 1
        received = {json.loads(row['body'])['text']: bool(row['archived']) for row in inbox}
        checks = {
            'receiver_subscribed_before_sender_started': waiting_observed,
            'both_clients_completed': all(t['exit_code'] == 0 and not t['timed_out'] and not t['client_error'] for t in self.turns),
            'challenge_delivered_once': bodies.count(challenge) == 1,
            'reply_delivered_once': bodies.count('ACK ' + challenge) == 1,
            'four_unique_operations': sorted(keys) == sorted(['codex-challenge', 'claude-reply', 'codex-resumed', 'claude-resumed']),
            'codex_resumed_same_session': resumed_codex['session_id'] == first_codex['session_id'],
            'claude_resumed_same_session': resumed_claude['session_id'] == first_claude['session_id'],
            'codex_recalled_unrepeated_marker': markers['codex'] in bodies,
            'claude_recalled_unrepeated_marker': markers['claude'] in bodies,
            'reply_persisted_before_codex_resume': any(row['to_agent'] == 'codex' for row in before_resume['inbox']),
            'new_message_persisted_while_claude_offline': any(json.loads(row['body'])['text'] == markers['codex'] for row in before_claude_resume['inbox']),
            'one_task_owner': len(before_resume['tasks']) == 1 and before_resume['tasks'][0]['owner'] in ('claude', 'codex'),
            'one_lock_owner': len(before_resume['locks']) == 1 and before_resume['locks'][0]['locked_by'] in ('claude', 'codex'),
            'both_clients_attempted_task_and_lock': all(tool_counts[a].get('claim_task') == 1 and tool_counts[a].get('acquire_lock') == 1 for a in ('claude', 'codex')),
            'sender_retried': tool_counts['codex'].get('post_message', 0) >= 3,
            'receiver_retried_reply': tool_counts['claude'].get('reply_message', 0) >= 3,
            'deliveries_acknowledged_by_both_clients': all(received.get(body) for body in (challenge, 'ACK ' + challenge, markers['codex'])),
            'task_completed': final['tasks'][0]['status'] == 'done',
            'lock_released': not final['locks'],
        }
        evidence = {
            'checks': checks,
            'versions': {actor: subprocess.check_output([actor, '--version'], text=True).strip() for actor in ('claude', 'codex')},
            'turns': [{k: v for k, v in turn.items() if k != 'calls'} | {'tools': [call['tool'] for call in turn['calls']]} for turn in self.turns],
            'task_owner': before_resume['tasks'][0]['owner'],
            'lock_owner': before_resume['locks'][0]['locked_by'],
            'delivery_count': len(inbox),
            'delivery_states': final['inbox_delivery_state'],
        }
        (self.output / 'evidence.json').write_text(json.dumps(evidence, indent=2))
        print(json.dumps(evidence, indent=2), flush=True)
        if not all(checks.values()):
            raise RuntimeError('Some acceptance checks failed; inspect private traces')

    def close(self):
        if hasattr(self, "http"):
            self.http.close()
        if hasattr(self, "hub"):
            self.hub.terminate()
            try:
                self.hub.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.hub.kill()
                self.hub.wait(timeout=5)
            self.log.close()


def verify_artifacts(output: Path) -> dict:
    """Audit persisted effects and tool attempts without making model requests."""
    evidence = json.loads((output / 'evidence.json').read_text())
    turns = json.loads((output / 'turns.json').read_text())
    with sqlite3.connect((output / 'runtime/data/bus.db').resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        deliveries = [dict(row) for row in db.execute('SELECT * FROM inbox')]
        operations = [dict(row) for row in db.execute('SELECT * FROM message_idempotency')]
    counts = {}
    for turn in turns:
        actor = counts.setdefault(turn['actor'], {})
        for call in turn['calls']:
            name = call['tool'].removeprefix('mcp__agent_bus__')
            actor[name] = actor.get(name, 0) + 1
    checks = evidence['checks']
    # Validate actual HTTP ordering; the cursor lookup is not an SSE subscription.
    log = (output / 'hub.log').read_text()
    subscribed, posted = log.find('GET /inbox/claude/events HTTP/'), log.find('POST /messages HTTP/')
    checks.pop('receiver_subscribed_before_sender_started', None)
    checks['sse_subscription_before_message_post'] = 0 <= subscribed < posted
    checks['both_clients_attempted_task_and_lock'] = all(counts[a].get('claim_task') == 1 and counts[a].get('acquire_lock') == 1 for a in ('claude', 'codex'))
    checks['sender_retried'] = counts['codex'].get('post_message', 0) == 3
    checks['receiver_retried_reply'] = counts['claude'].get('reply_message', 0) == 3
    checks['three_processed_deliveries_acknowledged'] = len(deliveries) == 4 and sum(row['archived'] for row in deliveries) == 3
    checks['both_agents_acknowledged'] = {row['to_agent'] for row in deliveries if row['archived']} == {'claude', 'codex'}
    checks['four_persisted_idempotency_keys'] = len(operations) == 4
    checks['http_contention_has_one_winner'] = (
        log.count('POST /tasks/T13-RACE/claim HTTP/1.1" 200') == 1
        and log.count('POST /tasks/T13-RACE/claim HTTP/1.1" 409') == 1
        and log.count('POST /locks/acquire HTTP/1.1" 200') == 1
        and log.count('POST /locks/acquire HTTP/1.1" 409') == 1
    )
    checks['no_shell_or_file_actions'] = True
    for trace in output.glob('*.jsonl'):
        for line in trace.read_text().splitlines():
            event = json.loads(line)
            item = event.get('item', {})
            if item.get('type') in ('command_execution', 'file_change', 'web_search'):
                checks['no_shell_or_file_actions'] = False
            for item in event.get('message', {}).get('content', []) or []:
                if item.get('type') == 'tool_use' and not item.get('name', '').startswith('mcp__agent_bus__'):
                    checks['no_shell_or_file_actions'] = False
    evidence.pop('delivery_states', None)
    evidence['tool_counts'] = counts
    evidence['acknowledged_deliveries'] = sum(row['archived'] for row in deliveries)
    evidence['final_pending_delivery'] = 'Final reply from claude to codex; scenario ends after sending it.'
    evidence['verified_at_utc'] = datetime.now(timezone.utc).isoformat()
    evidence['trace_sha256'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sorted(output.glob('*.jsonl'))}
    evidence['limitations'] = [
        'Claude Code uses the configured GLM-5.1 model, not an Anthropic model in this run.',
        'Headless clients invoked and resumed by this harness; no spontaneous TUI activation tested.',
        'SSE waiting exercised in Claude Code; Codex receives pending messages after explicit resume.',
        'Stop hooks and native AgentRunner adapters are outside this acceptance run.',
    ]
    if not all(checks.values()):
        raise RuntimeError(f"Failed artifact checks: {[key for key, ok in checks.items() if not ok]}")
    return evidence


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--smoke", action="store_true", help="Only verify tool discovery/status")
    parser.add_argument("--verify-output", type=Path, help="Audit an existing completed run without model calls")
    parser.add_argument("--publish-evidence", type=Path, help="Write the sanitized evidence from --verify-output")
    args = parser.parse_args()
    if args.verify_output:
        evidence = verify_artifacts(args.verify_output)
        if args.publish_evidence:
            args.publish_evidence.parent.mkdir(parents=True, exist_ok=True)
            args.publish_evidence.write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps(evidence, indent=2))
        return
    if args.publish_evidence:
        parser.error('--publish-evidence requires --verify-output')
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="agent-bus-t13-"))
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    harness = Acceptance(output)
    print(f"Private acceptance artifacts: {output}", flush=True)
    try:
        harness.prepare()
        if args.smoke:
            records = await asyncio.gather(*[harness.turn(actor, "smoke", "Call agent_bus get_project_status exactly once. Report its project_id and stop. Use only MCP; no shell or file operations.") for actor in ("claude", "codex")])
            if any(record['exit_code'] or record['timed_out'] or record['client_error'] or not record['calls'] for record in records):
                raise RuntimeError('A client failed the smoke check')
        else:
            await harness.scenario()
    finally:
        harness.close()


if __name__ == "__main__":
    asyncio.run(main())
