"""Tests de agent-bus watch (T13)."""

from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from agent_bus.cli.watch_cmds import (
    build_prompt,
    extract_session_id,
    load_session_map,
    run_turn,
    save_session_map,
    watch,
)


def test_extract_session_id():
    out = json.dumps({"session_id": "abc-123", "result": "ok"})
    assert extract_session_id(out) == "abc-123"
    assert extract_session_id("no json") is None
    assert extract_session_id(json.dumps({"result": "ok"})) is None


def test_extract_response_text():
    from agent_bus.cli.watch_cmds import extract_response_text

    out = json.dumps({"result": "Respuesta generada"})
    assert extract_response_text(out) == "Respuesta generada"
    assert extract_response_text("Texto plano") == "Texto plano"


def test_build_prompt_incluye_remitente_y_tarea():
    msg = {
        "from_agent": "agy",
        "body": {"text": "¿JWT o cookie?"},
        "related_task": "T2",
    }
    prompt = build_prompt(msg)
    assert "agy" in prompt
    assert "JWT o cookie" in prompt
    assert "T2" in prompt
    assert "agent-bus work msg agy" in prompt


def test_session_map_persistencia(tmp_path):
    f = tmp_path / "sessions.json"
    save_session_map({"t1": "s1"}, f)
    assert load_session_map(f) == {"t1": "s1"}
    # archivo corrupto no rompe
    f.write_text("{{{")
    assert load_session_map(f) == {}


@pytest.mark.asyncio
async def test_run_turn_dry_run(tmp_path):
    msg = {"from_agent": "agy", "body": {"text": "hola"}, "message_id": "m1"}
    smap = {}
    sid = await run_turn("claude", msg, smap, dry_run=True, sessions_file=tmp_path / "s.json")
    assert sid is None
    assert smap == {}


@pytest.mark.asyncio
async def test_run_turn_registra_session_del_cli(tmp_path, monkeypatch):
    """El session_id que devuelve el CLI se guarda en el mapping del thread."""
    import agent_bus.cli.watch_cmds as wc
    import shutil as shutil_mod

    msg = {
        "from_agent": "agy",
        "body": {"text": "hola"},
        "message_id": "m1",
        "metadata": {"thread_id": "thread-x"},
    }

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"session_id": "sess-new"})
        stderr = ""

    async def fake_run(*a, **kw):
        return FakeResult()

    def fake_which(cli):
        return "/usr/bin/claude"

    monkeypatch.setattr(wc.subprocess, "run", lambda *a, **kw: FakeResult())
    monkeypatch.setattr(wc.shutil, "which", fake_which)

    smap = {}
    sid = await run_turn("claude", msg, smap, dry_run=False, sessions_file=tmp_path / "s.json")
    assert sid == "sess-new"
    assert smap["thread-x"] == "sess-new"
    assert load_session_map(tmp_path / "s.json") == {"thread-x": "sess-new"}


@pytest.mark.asyncio
async def test_run_turn_reusa_session_del_thread(tmp_path, monkeypatch):
    """Con session existente, el turno usa --resume."""
    import agent_bus.cli.watch_cmds as wc

    captured: dict = {}

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"session_id": "sess-old"})
        stderr = ""

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        return FakeResult()

    monkeypatch.setattr(wc.subprocess, "run", fake_run)
    monkeypatch.setattr(wc.shutil, "which", lambda c: "/usr/bin/claude")

    msg = {"from_agent": "agy", "body": {"text": "hola"}, "metadata": {"thread_id": "t1"}}
    smap = {"t1": "sess-old"}
    sid = await run_turn("claude", msg, smap, dry_run=False, sessions_file=tmp_path / "s.json")
    assert sid == "sess-old"
    assert "--resume" in captured["cmd"]
    assert "sess-old" in captured["cmd"]


def test_watch_cli_requiere_agente(monkeypatch):
    monkeypatch.delenv("AGENT_BUS_AGENT_ID", raising=False)
    monkeypatch.setattr(
        "agent_bus.cli.display.get_current_agent", lambda: None
    )
    runner = CliRunner()
    result = runner.invoke(watch, [], catch_exceptions=False)
    assert "No hay agente" in result.output
