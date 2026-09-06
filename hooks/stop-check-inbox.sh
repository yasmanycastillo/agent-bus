#!/usr/bin/env bash
# Claude Code Stop hook. Use the installed agent-bus interpreter, not system Python.
# If copied outside this repository, set AGENT_BUS_PACKAGE_DIR to its installation.
BUS_URL="${AGENT_BUS_URL:-http://127.0.0.1:8420}"
AGENT_ID="${AGENT_BUS_AGENT_ID:-}"
BUS_PACKAGE_ROOT="${AGENT_BUS_PACKAGE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

if command -v agent-bus >/dev/null 2>&1; then
    hook_cmd=(agent-bus hook-inbox --bus-url "$BUS_URL" --agent "$AGENT_ID")
elif command -v uv >/dev/null 2>&1 && [[ -f "$BUS_PACKAGE_ROOT/pyproject.toml" ]]; then
    hook_cmd=(uv run --no-sync --project "$BUS_PACKAGE_ROOT" agent-bus hook-inbox --bus-url "$BUS_URL" --agent "$AGENT_ID")
else
    exit 0
fi

# Bound startup as well as HTTP on systems with timeout; the CLI also has a deadline.
if command -v timeout >/dev/null 2>&1; then
    timeout 6s "${hook_cmd[@]}" 2>/dev/null || true
else
    "${hook_cmd[@]}" 2>/dev/null || true
fi
