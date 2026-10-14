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


def test_ui_cmd_arranca_y_abre_navegador(tmp_path, monkeypatch):
    """`agent-bus ui` reusa/arranca el daemon y abre /console con la sesión admin ya iniciada."""
    import json
    import stat
    from urllib.parse import unquote, urlsplit

    import httpx

    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BUS_DATABASE_PATH", str(tmp_path / "bus.db"))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "test-project")
    monkeypatch.setenv("AGENT_BUS_URL", "http://127.0.0.1:8421")
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    started = []
    monkeypatch.setattr("agent_bus.cli.main._start_daemon", lambda host, port: started.append((host, port)))
    accepted = set()

    def fake_get(url, timeout=None, headers=None):
        if url.endswith("/auth/me"):  # the hub accepts only the sessions this test marks
            token = headers["Authorization"].removeprefix("Bearer ")
            return httpx.Response(200, json={"role": "admin"}) if token in accepted else httpx.Response(401)
        return httpx.Response(200, json={"status": "ok"})
    monkeypatch.setattr(httpx, "get", fake_get)

    def launch():
        res = CliRunner().invoke(app, ["ui"])
        assert res.exit_code == 0, res.output
        assert "Consola disponible en http://127.0.0.1:8421/console" in res.output
        page = urlsplit(opened[-1])
        assert page.scheme == "file"  # the token stays out of the browser's argv
        path = tmp_path / "credentials/console.html"
        assert unquote(page.path) == str(path) and stat.S_IMODE(path.stat().st_mode) == 0o600
        session = json.loads((tmp_path / "credentials/human.json").read_text())
        assert session["role"] == "admin" and session["token"] not in res.output
        assert json.dumps(f"http://127.0.0.1:8421/console#token={session['token']}") in path.read_text()
        return session["token"]

    first = launch()  # no saved session: provisions one
    assert started == [("127.0.0.1", 8421)]
    accepted.add(first)
    assert launch() == first  # the hub still accepts it: reused
    accepted.clear()
    assert launch() != first  # revoked or expired: replaced


def test_ui_cmd_no_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path))
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
    assert not (tmp_path / "credentials").exists()  # no browser, no session to hand over
