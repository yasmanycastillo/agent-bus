#!/usr/bin/env bash
# Hook Stop de Claude Code (MCP-2): al quedar la sesión idle, si hay mensajes
# pendientes que requieren respuesta en agent-bus, se lo inyecta al contexto
# para que la sesión los procese en su próximo turno — sin intervención humana.
#
# Configuración (.claude/settings.json del proyecto):
#   {"hooks": {"Stop": [{"hooks": [{"type": "command",
#      "command": "bash hooks/stop-check-inbox.sh"}]}]]}}

BUS_URL="${AGENT_BUS_URL:-http://localhost:8420}"
AGENT_ID="${AGENT_BUS_AGENT_ID:-claude}"

exec python3 - "$BUS_URL" "$AGENT_ID" <<'PYEOF'
import json
import sys
import signal

signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
signal.alarm(4)

bus_url, agent_id = sys.argv[1], sys.argv[2]
try:
    from agent_bus.security import sync_bus_client
    with sync_bus_client(agent_id, base_url=bus_url, timeout=3) as client:
        response = client.get(f"/inbox/{agent_id}")
        response.raise_for_status()
        msgs = response.json()
except Exception:
    sys.exit(0)  # bus caído: no bloquear la sesión

pending = [m for m in msgs if m.get("reply_needed")]
if not pending:
    sys.exit(0)  # nada pendiente: la sesión puede dormir

lines = [
    f"- {m['from_agent']}: {str((m.get('body') or {}).get('text', ''))[:80]}"
    for m in pending[-3:]
]
reason = (
    f"Tienes {len(pending)} mensaje(s) en agent-bus que requieren respuesta. "
    f"Lee: agent-bus work inbox; responde: agent-bus work msg <agente> \"<respuesta>\".\n"
    + "\n".join(lines)
)
print(json.dumps({"decision": "block", "reason": reason}))
PYEOF
