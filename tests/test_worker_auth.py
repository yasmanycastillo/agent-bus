"""Tests de worker/auth.py (T9)."""

from __future__ import annotations

import stat

from agent_bus.worker.auth import WorkerAuth, canonical_payload


def test_get_or_create_genera_y_persiste(tmp_path):
    auth = WorkerAuth(keys_dir=tmp_path)
    creds = auth.get_or_create("claude")
    assert len(creds.private_key_hex) == 64
    assert len(creds.public_key_hex) == 64
    # segunda llamada devuelve la misma clave (persistida)
    again = auth.get_or_create("claude")
    assert again.private_key_hex == creds.private_key_hex
    assert again.public_key_hex == creds.public_key_hex


def test_clave_privada_con_permisos_restringidos(tmp_path):
    auth = WorkerAuth(keys_dir=tmp_path)
    auth.get_or_create("claude")
    mode = stat.S_IMODE((tmp_path / "claude.key").stat().st_mode)
    assert mode == 0o600


def test_sign_y_verify_operacion(tmp_path):
    auth = WorkerAuth(keys_dir=tmp_path)
    headers = auth.sign_operation("claude", "POST", "/tasks/T1/claim", {"agent_id": "claude"})
    assert headers["X-Agent-Id"] == "claude"
    assert "X-Agent-Signature" in headers
    assert auth.verify_operation(
        "claude", "POST", "/tasks/T1/claim", {"agent_id": "claude"},
        headers["X-Agent-Signature"],
    )


def test_verify_rechaza_firma_de_otro_agente(tmp_path):
    auth = WorkerAuth(keys_dir=tmp_path)
    auth.get_or_create("claude")
    headers = auth.sign_operation("claude", "POST", "/messages", {"x": 1})
    # la firma de claude no verifica como si fuera de agy
    assert not auth.verify_operation("agy", "POST", "/messages", {"x": 1}, headers["X-Agent-Signature"])


def test_verify_rechaza_body_alterado(tmp_path):
    auth = WorkerAuth(keys_dir=tmp_path)
    headers = auth.sign_operation("claude", "POST", "/messages", {"amount": 1})
    # payload distinto al firmado
    assert not auth.verify_operation(
        "claude", "POST", "/messages", {"amount": 999999}, headers["X-Agent-Signature"]
    )


def test_verify_rechaza_agente_no_registrado(tmp_path):
    auth = WorkerAuth(keys_dir=tmp_path)
    assert not auth.verify_operation("desconocido", "POST", "/x", {}, "deadbeef")


def test_verify_con_public_key_explicita(tmp_path):
    auth = WorkerAuth(keys_dir=tmp_path)
    creds = auth.get_or_create("claude")
    headers = auth.sign_operation("claude", "GET", "/status")
    assert auth.verify_operation("claude", "GET", "/status", None, headers["X-Agent-Signature"], creds.public_key_hex)


def test_canonical_payload_determinista():
    a = canonical_payload("x", "post", "/p", {"b": 1, "a": 2})
    b = canonical_payload("x", "POST", "/p", {"a": 2, "b": 1})
    assert a == b  # orden de claves y mayúsculas no afectan
    assert b"agent_id" in a
