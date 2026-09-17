# Conectar Hermes Agent

Hermes puede utilizar agent-bus como cliente MCP. Preparar su identidad es igual
que para otros clientes:

```sh
agent-bus --project /ruta/mi-proyecto onboard --mcp-only \
  --agents hermes:hermes,reviewer:codex
```

Copia el servidor del archivo `.agent-bus/runtime/mcp/hermes.json` a la
configuración MCP de Hermes, usando los campos `command`, `args` y `env` en el
formato que admite tu versión. Habilita las herramientas descubiertas, reconecta
y pide `bootstrap_agent({})`. Comprueba la identidad y el proyecto devueltos.
No copies el token al repositorio ni compartas la credencial del revisor.

Para solicitar una revisión, usa `post_message` con destinatario `reviewer`,
`reply_needed=true` y una clave de idempotencia estable. Espera con
`wait_for_updates` y confirma la respuesta una vez procesada. El revisor necesita
su listener activo para responder automáticamente.

La clase Python `HermesOrchestrator` es un adaptador HTTP separado; no ejecuta el
CLI de Hermes ni configura su conexión MCP. No existe un worker Hermes ni un
lanzador `watch` nativo para ese proveedor. La publicación de desgloses requiere
la integración HTTP; no hay una herramienta MCP para crear esos desgloses.

Consulta [configuración MCP](mcp-setup.md), [mensajería](messaging.md) y
[pruebas opcionales con proveedores](development.md#pruebas-opcionales-con-proveedores-reales).
