#!/usr/bin/env bash
# Adapter for `agent-bus watch --cli /absolute/path/to/this/script`.
# The watcher supplies -p, --output-format and, when resuming, --resume.
# Run watch from the intended project with its agent-bus identity configured.
set -euo pipefail
exec grok --cwd "$PWD" --model "${GROK_WATCH_MODEL:-grok-4.6}" \
  --no-subagents --disable-web-search --tools search_tool,use_tool \
  --permission-mode dontAsk --allow search_tool --allow use_tool \
  --allow 'agent-bus-pilot__*' --allow 'mcp__agent-bus-pilot__*' \
  --max-turns 8 "$@"
