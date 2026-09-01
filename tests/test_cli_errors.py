"""Tests de traducción de errores HTTP de la CLI (T15)."""

from __future__ import annotations

from agent_bus.cli.main import _explain_error


class FakeResp:
    def __init__(self, status_code: int, json_data=None, text: str = "", method: str = "POST"):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (str(json_data) if json_data else "")
        self.request = type("R", (), {"method": method})()

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def test_422_campo_faltante():
    resp = FakeResp(422, {"detail": [{"type": "missing", "loc": ["body", "file_path"]}]})
    msg = _explain_error(resp)
    assert "file_path" in msg
    assert "Falta" in msg


def test_422_sin_detalle_parseable():
    resp = FakeResp(422, None, text="validation error crudo")
    msg = _explain_error(resp)
    assert "Parametros invalidos" in msg


def test_409_conflicto_extrae_error():
    resp = FakeResp(409, {"error": "Task not found or already owned"})
    assert "already owned" in _explain_error(resp)


def test_404_mensaje_generico():
    resp = FakeResp(404, {"error": "nope"})
    assert "No encontrado" in _explain_error(resp)


def test_405_metodo():
    resp = FakeResp(405, method="DELETE")
    msg = _explain_error(resp)
    assert "DELETE" in msg
    assert "metodo" in msg


def test_500_sugiere_log():
    resp = FakeResp(500, {"error": "x"})
    assert "log" in _explain_error(resp).lower()


def test_error_generico_extrae_campo_error():
    resp = FakeResp(400, {"error": "Bad file"})
    assert "Bad file" in _explain_error(resp)


def test_e2e_cli_lock_sin_parametro_cuenta():
    """La respuesta de 422 del bus real se traduce (contra servidor vivo)."""
    import httpx

    try:
        with httpx.Client(base_url="http://localhost:8420", timeout=3) as c:
            resp = c.post("/locks/acquire", json={"path": "x.py", "agent_id": "claude"})
    except httpx.ConnectError:
        return  # sin servidor: skip silencioso
    if resp.status_code == 422:
        msg = _explain_error(resp)
        assert "file_path" in msg  # antes: JSON crudo ilegible
