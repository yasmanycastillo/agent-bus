# T-13 — Aceptación con clientes MCP reales

El 6 de septiembre de 2026 se ejecutó el escenario con **Claude Code 2.1.185** y **Codex CLI 0.153.4**, contra el código de T-12 (`ca847a8`). Ambos utilizaron el servidor MCP por stdio y un hub HTTP efímero, con credenciales independientes de agente. Resultados verificables: [evidence/t13.json](evidence/t13.json).

Claude Code utilizó el modelo **GLM-5.1** configurado en este entorno. Codex utilizó **gpt-6-astra**. Esta aceptación acredita las aplicaciones cliente y esa configuración; no es una prueba del modelo Anthropic Claude ni de todos los proveedores anunciados anteriormente.

## Matriz de capacidades ejercitadas

| Capacidad | Claude Code 2.1.185 / GLM-5.1 | Codex CLI 0.153.4 / gpt-6-astra |
| --- | --- | --- |
| Conexión MCP stdio y herramientas | Consulta inicial y operaciones reales correctas | Consulta inicial y operaciones reales correctas |
| Comunicación | Lee, responde y confirma; repetir la respuesta conserva una sola entrega | Envía con reintento, lee y confirma la respuesta |
| Espera | `wait_for_updates(120)` abrió SSE antes del envío y permitió continuar al recibir | Se recuperaron pendientes después de reanudar; no se ejercitó una espera SSE desde Codex en esta corrida |
| Recepción durante desconexión | Mensaje persistido mientras su proceso estaba terminado; recuperado al volver | Respuesta pendiente recuperada al reanudar |
| Continuación de conversación | `claude --resume <id> -p`; mismo ID y recuerdo de un marcador no repetido | `codex exec resume <id>`; mismo ID y recuerdo de un marcador no repetido |
| Ejecución sin TUI | Dos procesos headless sucesivos, iniciados por el harness | Dos procesos headless sucesivos, iniciados por el harness |
| Activación espontánea de una TUI | No probada; el harness inicia la reanudación | No probada; el harness inicia la reanudación |
| Runner del repositorio | No ejercitado en esta aceptación | No ejercitado: el alias `provider=codex` todavía invoca Aider |

AGY/Antigravity, Aider y Grok no participaron en esta aceptación. Los hooks Stop y el navegador tampoco se ejercitaron con una aplicación viva en esta tanda. Sus ejemplos no constituyen una garantía de compatibilidad.

## Escenario y resultados

1. El harness provisionó dos sesiones de agente y una administrativa, y creó una tarea en una base nueva. Sólo el operador de pruebas usó HTTP directo para preparar el escenario y leer evidencias.
2. Claude Code y Codex intentaron reclamar `T13-RACE` y adquirir el recurso compartido `shared.txt`. HTTP registró exactamente un `200` y un `409` para cada competencia. Claude obtuvo ambos recursos; Codex continuó sin ejecutar una tarea que no poseía.
3. Claude abrió una espera SSE. Codex envió un desafío y repitió exactamente el envío con la misma clave. Claude leyó el mensaje, respondió con el contenido recibido y repitió exactamente la respuesta con confirmación. Se conservó una entrega por operación lógica.
4. Tras terminar ambos procesos, el harness reanudó la conversación de Codex. Este leyó y confirmó la respuesta, y envió un marcador recordado de su turno anterior mientras Claude permanecía desconectado.
5. Claude se reanudó con su ID original, recuperó y confirmó el mensaje, respondió con su propio marcador recordado, liberó su adquisición y finalizó la tarea.

Los cuatro turnos finalizaron con código 0 y sin agotar su plazo. Duraciones: Codex intercambio 21,08 s, Claude intercambio 57,15 s, Codex reanudación 15,96 s y Claude reanudación 25,01 s. Los dos turnos iniciales se solaparon.

Resultado persistido: **4 operaciones lógicas, 4 entregas, 3 entregas confirmadas** y ningún lock restante. La última respuesta de Claude a Codex queda pendiente porque el escenario termina al enviarla; no se contabiliza como confirmada. No hubo retransmisión humana del texto ni copia del mensaje de un cliente al prompt del otro. El harness sí controla cuándo inicia/reanuda cada proceso y prepara la tarea.

## Reproducir

Se necesitan los dos ejecutables instalados y autenticados. Esta prueba hace llamadas reales a los modelos configurados y consume la cuota de las cuentas. No se ejecuta dentro de pytest ni en CI por defecto.

```bash
uv sync --locked --extra dev
claude --version
codex --version
claude auth status
codex login status

# Diagnóstico mínimo de descubrimiento y estado:
uv run --locked python scripts/acceptance_real_clients.py --smoke

# Escenario completo:
uv run --locked python scripts/acceptance_real_clients.py
```

El script imprime una ruta temporal privada. Allí conserva credenciales `0600`, base, configuración de prueba, trazas JSONL, stderr y evidencia. Cada ejecución crea un proyecto Git nuevo, usa un puerto loopback libre y cierra sólo su propio hub al terminar. No utiliza el hub personal de 8420 ni modifica la configuración global MCP. Las conversaciones externas se persisten para poder probar `resume`.

La configuración de Codex se pasa por argumentos con `--ignore-user-config` y MCP obligatorio. Claude recibe `--strict-mcp-config`, sólo las herramientas MCP del bus autorizadas y herramientas incorporadas deshabilitadas. Ambos procesos reciben rutas absolutas de proyecto, runtime, credencial y código del servidor. Se permiten hasta 240 segundos por turno; una espera MCP puede durar hasta 120 segundos. No se utilizan los adaptadores `AgentRunner`.

Para auditar de nuevo una corrida terminada, sin llamadas a modelos:

```bash
uv run --locked python scripts/acceptance_real_clients.py \
  --verify-output /ruta/temporal/impresa \
  --publish-evidence /ruta/a/evidencia-publicable.json
```

La evidencia publicable contiene versiones, herramientas, comprobaciones, tiempos e identificadores de conversación, y hashes SHA-256 de las trazas. No contiene credenciales, tokens de adquisición ni los prompts completos. Las trazas y la base permanecen locales y no se añaden a Git. La corrida registrada dejó sus artefactos en `/tmp/agent-bus-t13-n21ax5md`; esa ubicación es temporal y no es requisito para repetir el escenario.

## Límites y documentación de referencia

La recuperación de mensajes pertenece al hub; la ejecución de un turno pertenece al cliente. Una llamada MCP pendiente puede continuar cuando recibe datos, pero un proceso finalizado necesita ser invocado otra vez. Esta tanda verifica continuación de conversación mediante procesos sucesivos, no inserción de turnos en una TUI ya abierta. La recuperación persistente de workers y sus adaptadores sigue en T-14; la integración Git autónoma, en T-15.

La configuración se contrastó con la [documentación oficial de MCP de Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) y su [modo no interactivo y reanudación](https://learn.chatgpt.com/docs/non-interactive-mode). Las opciones de Claude Code se comprobaron con `claude --help` de la versión instalada. Las afirmaciones de aceptación de esta página se basan en la ejecución registrada, no sólo en esas referencias.
