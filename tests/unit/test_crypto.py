from __future__ import annotations

from agent_bus.crypto import canonicalize, generate_keypair, sign_message, verify_signature
from agent_bus.types import Envelope, MessageType


def test_generate_keypair():
    private_hex, public_hex = generate_keypair()
    assert len(private_hex) == 64  # 32 bytes hex
    assert len(public_hex) == 64


def test_sign_and_verify():
    private_hex, public_hex = generate_keypair()
    envelope = Envelope(
        from_agent="claude",
        to_agent="codex",
        message_type=MessageType.INBOX,
        body={"text": "hello"},
    )
    signature = sign_message(private_hex, envelope)
    assert verify_signature(public_hex, signature, envelope)


def test_verify_fails_with_wrong_key():
    private_hex, _ = generate_keypair()
    _, other_public_hex = generate_keypair()
    envelope = Envelope(
        from_agent="claude",
        message_type=MessageType.BROADCAST,
    )
    signature = sign_message(private_hex, envelope)
    assert not verify_signature(other_public_hex, signature, envelope)


def test_canonicalize_deterministic():
    env1 = Envelope(from_agent="a", message_type=MessageType.INBOX)
    env2 = Envelope(
        message_id=env1.message_id,
        from_agent="a",
        message_type=MessageType.INBOX,
        timestamp=env1.timestamp,
    )
    assert canonicalize(env1) == canonicalize(env2)


def test_signature_changes_if_envelope_changes():
    private_hex, public_hex = generate_keypair()
    envelope = Envelope(from_agent="claude", message_type=MessageType.INBOX)
    sig1 = sign_message(private_hex, envelope)

    modified = envelope.model_copy(update={"body": {"changed": True}})
    sig2 = sign_message(private_hex, modified)

    assert not verify_signature(public_hex, sig1, modified)
    assert verify_signature(public_hex, sig2, modified)
