from __future__ import annotations

import json
from typing import Any

from nacl.signing import SigningKey, VerifyKey
from nacl.encoding import HexEncoder
from nacl.exceptions import BadSignatureError

from agent_bus.types import Envelope


def generate_keypair() -> tuple[str, str]:
    """Generate Ed25519 keypair. Returns (private_key_hex, public_key_hex)."""
    signing_key = SigningKey.generate()
    private_hex = signing_key.encode(encoder=HexEncoder).decode()
    public_hex = signing_key.verify_key.encode(encoder=HexEncoder).decode()
    return private_hex, public_hex


def canonicalize(envelope: Envelope) -> bytes:
    """Deterministic JSON serialization for signing."""
    data: dict[str, Any] = {
        "message_id": envelope.message_id,
        "from_agent": envelope.from_agent,
        "to_agent": envelope.to_agent,
        "message_type": envelope.message_type.value,
        "timestamp": envelope.timestamp.isoformat(),
        "body": envelope.body,
    }
    if envelope.correlation_id:
        data["correlation_id"] = envelope.correlation_id
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def sign_message(private_key_hex: str, envelope: Envelope) -> str:
    """Sign an envelope's canonical form with the given private key."""
    signing_key = SigningKey(private_key_hex, encoder=HexEncoder)
    message_bytes = canonicalize(envelope)
    signed = signing_key.sign(message_bytes)
    return signed.signature.hex()


def verify_signature(public_key_hex: str, signature_hex: str, envelope: Envelope) -> bool:
    """Verify an envelope signature against a public key."""
    try:
        verify_key = VerifyKey(public_key_hex, encoder=HexEncoder)
        message_bytes = canonicalize(envelope)
        verify_key.verify(message_bytes, bytes.fromhex(signature_hex))
        return True
    except (BadSignatureError, Exception):
        return False
