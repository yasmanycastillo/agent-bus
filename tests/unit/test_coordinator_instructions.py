"""The MCP tells a coordinator to assign real agents."""
from agent_bus.core.coordination import INSTRUCTIONS
from agent_bus.mcp.coordination import TOOL_GUIDANCE
from agent_bus.mcp.server import TOOLS_DEFINITIONS


def test_instructions_send_the_work_to_real_agents():
    for name in ("hermes", "grok", "claude", "codex", "agy"):
        assert name in INSTRUCTIONS
    assert "subagente" in INSTRUCTIONS
    assert "submit_instruction" in INSTRUCTIONS
    assert "assign_work" in INSTRUCTIONS
    assert "no asigna" in INSTRUCTIONS
    assert "run-team" in INSTRUCTIONS
    assert "no lances workers" in INSTRUCTIONS


class _Hub:
    def __init__(self, body):
        self.body = body

    async def post(self, url, json=None):
        return self

    def raise_for_status(self):
        return None

    def json(self):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _Server:
    session = {"agent_id": "codex"}
    agent_id = "codex"
    project_id = "traza"

    def __init__(self, body):
        self.hub = _Hub(body)

    def _client(self):
        return self.hub


async def test_bootstrap_keeps_the_package_instructions_when_the_hub_is_old():
    from agent_bus.mcp.coordination import execute

    result = await execute(_Server({"instructions": "haz el trabajo e inicia workers", "project_id": "traza"}), "bootstrap_agent", {})
    assert result["project_id"] == "traza"
    assert result["instructions"] == INSTRUCTIONS
    assert "run-team" in result["instructions"]


def test_assign_tools_reject_subagents():
    descriptions = {tool["name"]: tool["description"] for tool in TOOLS_DEFINITIONS}
    for name in ("submit_instruction", "assign_work"):
        assert "subagente" in descriptions[name]
        assert "subagente" in TOOL_GUIDANCE[name]
