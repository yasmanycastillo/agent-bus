from __future__ import annotations

from agent_bus.crypto import verify_signature
from agent_bus.types import Envelope


async def verify_envelope_signature(envelope: Envelope, public_key: str) -> bool:
    """Verify an envelope's signature. Returns False if no signature present."""
    if not envelope.signature:
        return False
    return verify_signature(public_key, envelope.signature, envelope)
