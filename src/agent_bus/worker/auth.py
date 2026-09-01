"""Autenticación Ed25519 del worker (T9, sección 8.3 integrada).

El daemon worker firma sus operaciones de escritura con la clave privada de
su agente; el hub valida contra las claves públicas registradas. Un worker
solo puede operar como un agente registrado con clave válida.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agent_bus.config import DEFAULT_CONFIG_DIR
from agent_bus.crypto import generate_keypair


@dataclass
class WorkerCredentials:
    agent_id: str
    private_key_hex: str
    public_key_hex: str

    def key_path(self) -> Path:
        return DEFAULT_CONFIG_DIR / "agents" / f"{self.agent_id}.key"


def canonical_payload(agent_id: str, method: str, path: str, body: dict | None) -> bytes:
    """Serialización determinista de la operación a firmar."""
    data = {
        "agent_id": agent_id,
        "method": method.upper(),
        "path": path,
        "body": body if body is not None else {},
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def _sign(private_key_hex: str, payload: bytes) -> str:
    from nacl.signing import SigningKey
    from nacl.encoding import HexEncoder

    signing_key = SigningKey(private_key_hex, encoder=HexEncoder)
    return signing_key.sign(payload).signature.hex()


def _verify(public_key_hex: str, payload: bytes, signature_hex: str) -> bool:
    from nacl.signing import VerifyKey as SigningVerifyKey
    from nacl.encoding import HexEncoder
    from nacl.exceptions import BadSignatureError

    try:
        verify_key = SigningVerifyKey(public_key_hex, encoder=HexEncoder)
        verify_key.verify(payload, bytes.fromhex(signature_hex))
        return True
    except (BadSignatureError, ValueError, Exception):
        return False


class WorkerAuth:
    """Gestiona credenciales por agente y firma/verificación de operaciones."""

    def __init__(self, keys_dir: Path | None = None) -> None:
        self.keys_dir = keys_dir or DEFAULT_CONFIG_DIR / "agents"
        self.keys_dir.mkdir(parents=True, exist_ok=True)

    def _key_file(self, agent_id: str) -> Path:
        return self.keys_dir / f"{agent_id}.key"

    def _pub_file(self, agent_id: str) -> Path:
        return self.keys_dir / f"{agent_id}.pub"

    def get_or_create(self, agent_id: str) -> WorkerCredentials:
        """Carga la clave del agente o genera una nueva (primera vez)."""
        key_file = self._key_file(agent_id)
        pub_file = self._pub_file(agent_id)
        if key_file.exists() and pub_file.exists():
            return WorkerCredentials(agent_id, key_file.read_text().strip(), pub_file.read_text().strip())
        priv, pub = generate_keypair()
        key_file.write_text(priv)
        key_file.chmod(0o600)
        pub_file.write_text(pub)
        return WorkerCredentials(agent_id, priv, pub)

    def get_public_key(self, agent_id: str) -> str | None:
        pub_file = self._pub_file(agent_id)
        return pub_file.read_text().strip() if pub_file.exists() else None

    def sign_operation(
        self, agent_id: str, method: str, path: str, body: dict | None = None
    ) -> dict:
        """Firma una operación HTTP y devuelve los headers de autenticación."""
        creds = self.get_or_create(agent_id)
        payload = canonical_payload(agent_id, method, path, body)
        signature = _sign(creds.private_key_hex, payload)
        return {
            "X-Agent-Id": agent_id,
            "X-Agent-Signature": signature,
            "X-Agent-Public-Key": creds.public_key_hex,
        }

    def verify_operation(
        self,
        agent_id: str,
        method: str,
        path: str,
        body: dict | None,
        signature_hex: str,
        public_key_hex: str | None = None,
    ) -> bool:
        """Verifica la firma de una operación.

        Si ``public_key_hex`` no se pasa, usa la clave pública registrada del
        agente (raíz de confianza local). Falla si el agente no está registrado.
        """
        key = public_key_hex or self.get_public_key(agent_id)
        if not key or not signature_hex:
            return False
        payload = canonical_payload(agent_id, method, path, body)
        return _verify(key, payload, signature_hex)
