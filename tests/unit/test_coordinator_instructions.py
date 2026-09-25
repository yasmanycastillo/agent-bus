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


def test_assign_tools_reject_subagents():
    descriptions = {tool["name"]: tool["description"] for tool in TOOLS_DEFINITIONS}
    for name in ("submit_instruction", "assign_work"):
        assert "subagente" in descriptions[name]
        assert "subagente" in TOOL_GUIDANCE[name]
