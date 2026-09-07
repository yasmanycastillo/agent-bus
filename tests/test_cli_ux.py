from __future__ import annotations

from click.testing import CliRunner

from agent_bus.cli.main import app


def test_top_cmd_once(live_bus_url, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_URL", live_bus_url)
    runner = CliRunner()
    res = runner.invoke(app, ["top", "--once"])
    assert res.exit_code == 0
    assert "top" in res.output
    assert "Error" not in res.output


def test_quickstart_help():
    runner = CliRunner()
    res = runner.invoke(app, ["quickstart", "--help"])
    assert res.exit_code == 0
    assert "Onboarding en 1 solo paso" in res.output
    assert "--agents" in res.output
    assert "--mock" in res.output


def test_ui_cmd_arranca_y_abre_navegador(monkeypatch):
    """`agent-bus ui` reusa/arranca el daemon y abre /console en el navegador."""
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    started = []
    def fake_start_daemon(host, port):
        started.append((host, port))
    monkeypatch.setattr("agent_bus.cli.main._start_daemon", fake_start_daemon)

    import httpx
    def fake_get(url, timeout=None):
        return httpx.Response(200, json={"status": "ok"})
    monkeypatch.setattr(httpx, "get", fake_get)

    monkeypatch.setenv("AGENT_BUS_URL", "http://127.0.0.1:8421")
    runner = CliRunner()
    res = runner.invoke(app, ["ui"])
    assert res.exit_code == 0, res.output
    assert started == [("127.0.0.1", 8421)]
    assert opened and opened[0] == "http://127.0.0.1:8421/console"
    assert "Consola disponible" in res.output


def test_ui_cmd_no_browser(monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setattr("agent_bus.cli.main._start_daemon", lambda h, p: None)
    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: httpx.Response(200, json={}))
    monkeypatch.setenv("AGENT_BUS_URL", "http://127.0.0.1:8422")
    runner = CliRunner()
    res = runner.invoke(app, ["ui", "--no-browser"])
    assert res.exit_code == 0, res.output
    assert opened == []
