from pathlib import Path

import pytest

from agent_bus.mcp.join import is_agent_bus_source


@pytest.mark.parametrize("name, expected", [("agent-bus-coord", True), ("agent-bus", True), ("otro", False)])
def test_is_agent_bus_source_recognizes_distribution_names(tmp_path: Path, name: str, expected: bool) -> None:
    (tmp_path / "src" / "agent_bus").mkdir(parents=True)
    (tmp_path / "src" / "agent_bus" / "__init__.py").write_text("")
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    assert is_agent_bus_source(tmp_path) is expected


def test_this_checkout_is_agent_bus_source() -> None:
    assert is_agent_bus_source(Path(__file__).resolve().parents[2])
