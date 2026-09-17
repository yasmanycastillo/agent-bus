from __future__ import annotations

import pytest

from agent_bus.cli.setup_wizard import _validated_agent_ids


def test_setup_validates_every_identity_before_processing_any_agent():
    with pytest.raises(ValueError, match="Invalid identity"):
        _validated_agent_ids("alice,../../../escaped,bob")


def test_setup_normalizes_and_accepts_valid_unique_identities():
    assert _validated_agent_ids(" alice, codex-2,alice ") == ["alice", "codex-2"]