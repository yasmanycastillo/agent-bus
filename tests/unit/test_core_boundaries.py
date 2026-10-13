from pathlib import Path


def test_core_does_not_import_provider_clis():
    root = Path("src/agent_bus/core")
    banned = ("codex", "claude", "kimi", "openhands", "orca")
    for path in root.rglob("*.py"):
        text = path.read_text()
        for name in banned:
            assert f"import {name}" not in text
            assert f"from {name}" not in text
