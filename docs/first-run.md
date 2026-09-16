# Primer uso MCP

Esta guía prepara un hub local y sesiones para dos aplicaciones. No arranca
workers ni el integrador. La versión inicial se valida en Linux con Python 3.12+.
El paquete aún no tiene una release pública verificada en PyPI: no asumir que
`uvx agent-bus` descarga este proyecto.

## Instalar desde el repositorio

Con uv instalado, desde el checkout:

```sh
uv tool install .
```

Esto deja una instalación persistente y el comando `agent-bus` en el PATH de tools
de uv. Si la terminal no lo encuentra, ejecutar `uv tool update-shell` y abrir otra.
El usuario final podrá instalar una versión del paquete sin clonar cuando exista
la release pública. El flujo desde un wheel también está soportado por uv.

## Preparar tu proyecto

```sh
agent-bus --project /ruta/mi-proyecto onboard --mcp-only
```

Por defecto prepara `backend` (etiqueta Claude), `qa` (etiqueta Codex) e `integrator`
(admin). Las etiquetas no instalan ni ejecutan esos clientes. Personaliza:

```sh
agent-bus --project /ruta/mi-proyecto onboard --mcp-only \
  --agents backend:claude,qa:codex --admin human --port 8421
```

`--yes` autoriza explícitamente la provisión local sin preguntas. Las identidades
se validan antes de escribir; no usar identidades duplicadas ni compartir la del
administrador con un agente. El asistente conserva el hub configurado al repetir;
no cambia el puerto de un proyecto existente.

## Conectar tus aplicaciones

El asistente imprime un archivo JSON por agente en `.agent-bus/runtime/mcp/`.
Contiene el ejecutable Python instalado, argumentos y rutas absolutas a sesiones;
no incluye los tokens. Copiar la entrada `mcpServers.agent-bus` al formato JSON
MCP de tu aplicación, conservando las demás entradas. Para clientes con otro
formato, trasladar `command`, `args` y `env` según su documentación; no copiar
JSON directamente a un archivo TOML.

Conectar una aplicación como `backend` y otra como `qa`, reiniciar la conexión
y pedir `bootstrap_agent({})`. La comprobación HTTP del asistente confirma hub y
credenciales; el primer bootstrap del cliente confirma la conexión MCP completa.

Conservar el entorno Python usado por los snippets. Una reinstalación que cambie
su ruta requiere regenerar la configuración. El asistente no sobrescribe un
snippet diferente ni modifica archivos propios del cliente.

## Operación y recuperación

- Hub ocupado por otro proyecto: para un proyecto nuevo, elegir otro `--port`.
- Hub sin arrancar: consultar `.agent-bus/runtime/bus.log` y repetir.
- Credencial vencida/revocada: seguir [autenticación](authentication.md); no se
  reemplaza silenciosamente una sesión existente. Duración inicial: 24 horas.
- Variables `AGENT_BUS_*` de otro entorno: abrir una terminal limpia y seleccionar
  el proyecto mediante `--project`; se rechazan overrides ambiguos.
- Consola: abrir la URL `/console` que imprime el asistente. La sesión admin se
  guarda con permisos 0600; el token no se imprime. El acceso visual guiado forma
  parte de la siguiente etapa, no de la comprobación MCP del asistente.
- Detener el hub propio: `agent-bus --project /ruta/mi-proyecto serve --stop`.

No versionar `.agent-bus/runtime/`: contiene credenciales y datos operativos.
Ver [plan de lanzamiento](launch-plan.md) para los criterios pendientes.
