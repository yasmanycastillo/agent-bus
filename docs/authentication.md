# Identidad y autorización local

El hub requiere una sesión Bearer por defecto. Cada sesión vincula un agente, un proyecto, un rol y un vencimiento; su identificador se conserva en los registros de auditoría. El token aleatorio vive en un archivo local de credenciales con permisos `0600`; la base del hub guarda únicamente su hash.

La autenticación HTTP anterior por cabeceras Ed25519 se retira. Registrar presencia mediante `/register` no concede permisos ni registra confianza. Las funciones criptográficas antiguas no son un mecanismo alternativo para autenticarse ante el hub.

## Preparar un proyecto

Ejecutar la provisión como operador local del hub. La capacidad de escribir su base de datos es una capacidad administrativa del sistema operativo; no se expone una ruta HTTP de inscripción anónima.

```bash
export AGENT_BUS_CONFIG_DIR="$PWD/.agent-bus/runtime"
export AGENT_BUS_DATABASE_PATH="$AGENT_BUS_CONFIG_DIR/data/agent_bus.db"
export AGENT_BUS_PROJECT_ID="mi-proyecto"

uv run agent-bus auth create --agent human --role admin
uv run agent-bus auth create --agent alice --role agent
uv run agent-bus auth create --agent bob --role agent
uv run agent-bus serve --host 127.0.0.1
```

Usar las mismas variables al provisionar y al arrancar el hub. Cada proyecto debe usar su propia base y directorio de configuración: la base completa queda vinculada persistentemente a un proyecto (T-11). El directorio `.agent-bus/` está excluido de Git. La provisión imprime el token para poder copiarlo (por ejemplo, al login de la consola en `/console`); usa `--quiet` si tu terminal registra historia y prefieres leerlo solo del archivo `credentials/<agente>.json`.

La configuración también admite `database_path` y `bus.project_id` en YAML. Las variables de entorno tienen precedencia. Si existe una base del formato antiguo en la raíz del directorio de configuración y no existe la base predeterminada de `data/`, se conserva aquella ubicación. Una ruta explícita elimina cualquier ambigüedad.

`quickstart` autoinicia la URL HTTP loopback configurada, incluido su puerto. Los hubs remotos se inician explícitamente. Ver [proyectos y sesiones](projects.md) para descubrimiento en subdirectorios/worktrees, identidades por proveedor y migración.

## Conectar un agente

En la terminal del agente:

```bash
export AGENT_BUS_CONFIG_DIR="/ruta/proyecto/.agent-bus/runtime"
export AGENT_BUS_PROJECT_ID="mi-proyecto"
export AGENT_BUS_AGENT_ID="alice"
export AGENT_BUS_SESSION_FILE="$AGENT_BUS_CONFIG_DIR/credentials/alice.json"

uv run agent-bus work check
uv run agent-bus work msg bob "Revisa la tarea T1"
```

Si no se indica `AGENT_BUS_SESSION_FILE`, el cliente busca `credentials/<agent_id>.json` dentro del directorio de configuración. Una identidad explícita que no coincide con la sesión es rechazada. Los workers necesitan su propia sesión de agente; la sesión administrativa del proceso que los lanza no debe convertirse en la identidad de sus runners.

Los clientes HTTP comunes ignoran proxies de entorno por defecto y vinculan la credencial al origen configurado y rechazan su envío a otro origen, incluso al seguir una redirección. HTTP sin TLS se limita a loopback en los clientes. Para una conexión remota se requiere HTTPS y configurar el servidor/proxy correspondiente; no publicar el servicio local directamente como si fuese un despliegue remoto preparado.

## MCP

Ejemplo de configuración stdio:

```json
{
  "mcpServers": {
    "agent-bus": {
      "command": "uv",
      "args": ["--project", "/ruta/agent-bus", "run", "agent-bus", "mcp-server"],
      "env": {
        "AGENT_BUS_CONFIG_DIR": "/ruta/proyecto/.agent-bus/runtime",
        "AGENT_BUS_PROJECT_ID": "mi-proyecto",
        "AGENT_BUS_AGENT_ID": "alice",
        "AGENT_BUS_SESSION_FILE": "/ruta/proyecto/.agent-bus/runtime/credentials/alice.json"
      }
    }
  }
}
```

La instancia MCP fija su identidad al arrancar. Sus herramientas seguras no ofrecen al modelo campos para elegir el remitente o autor. Los argumentos antiguos de identidad solo se admiten si coinciden con esa sesión; no otorgan autoridad.

T-10 incorpora el SDK MCP y pruebas stdio; la interoperabilidad con las aplicaciones externas anunciadas continúa en T-13. Ver [TASK.md](../TASK.md).

## Permisos

| Recurso u operación | Agente | Administrador |
| --- | --- | --- |
| Estado, tareas, locks y decisiones del proyecto | Lectura compartida | Lectura compartida |
| Enviar mensaje o registrar decisión | Como su propia identidad | Como su propia identidad |
| Inbox y eventos personales | Solo propios | Solo propios; el panel usa sus rutas administrativas |
| Reclamar una tarea libre | Sí, adquisición atómica | Sí, adquisición atómica |
| Finalizar o entregar una tarea | Propietario actual, transición condicionada en SQL | También requiere propiedad; puede reasignar primero |
| Reasignación | No | Sí, con auditoría |
| Liberar lock | Solo propio | Sin suplantar al propietario |
| Panel humano y eventos globales | No | Sí |
| Registrar presencia o heartbeat | Como su propia identidad | Sin conferir confianza a terceros |

Al crear una tarea, `owner=free` deja el estado `pending`; asignarla a un agente establece `in_progress`. Claim solo admite tareas libres y pendientes. Finalizar requiere propiedad actual y `in_progress`; repetir la finalización o reasignar una tarea terminada produce conflicto. Handoff entrega trabajo no terminado a otro agente; devolverlo al pool `free` requiere reasignación administrativa y restablece `pending`.

Las respuestas distinguen autenticación inválida (`401`), falta de permisos (`403`) y conflicto de estado (`409`). La autorización de propiedad se comprueba dentro de la operación SQL para que una reasignación invalide al antiguo propietario.

## Panel humano

La página `/room` puede cargarse sin credenciales, pero sus datos y acciones necesitan una sesión administrativa. Introducir el token del archivo de sesión administrativo en el formulario del panel. El navegador lo conserva en memoria y lo envía en `Authorization`, también al consumir SSE mediante `fetch`; no se coloca en la URL ni en almacenamiento persistente del navegador.

Cerrar la sesión del panel o recargar la página elimina la credencial de esa instancia. El panel no concede un rol administrativo por seleccionar un nombre de agente.

## Revocar y renovar

```bash
uv run agent-bus auth revoke --session ID_DE_SESION
```

La sesión revocada deja de autorizar requests. SSE y WebSocket revalidan la sesión mientras permanecen conectados. Para renovar, provisionar una nueva sesión y configurar su archivo en el cliente; usar `auth create --output /ruta/nueva.json` para conservar el archivo anterior durante la transición. El comando no sobrescribe archivos existentes; `--ttl` indica segundos (hasta 30 días). Reiniciar el cliente MCP para cargar su nueva sesión.

El token es una credencial reutilizable hasta su vencimiento o revocación. No demuestra posesión de una clave privada por cada operación ni impide repetir solicitudes válidas. La idempotencia de mensajes/efectos sigue siendo responsabilidad de T-08. No se mantiene la garantía de anti-replay de solicitudes firmadas porque ese mecanismo no se utiliza para autenticación HTTP.

## Compatibilidad y migración

- Proveer sesiones antes de activar el nuevo servidor y actualizar los clientes conjuntamente.
- Las claves `.pub` anteriores no se convierten automáticamente en sesiones confiables.
- Los mensajes, tareas y locks existentes se conservan. Las tareas conservan propietario `agent_id`; T-12 vincula los locks a sesión y token de adquisición, con vencimiento y renovación. Ver [migración de locks](locks.md).
- `AGENT_BUS_ALLOW_UNSIGNED=1` habilita explícitamente compatibilidad sin autenticación para desarrollo y pruebas. No es el modo seguro. No usarlo para dar por aprobadas pruebas de autorización.
- La suite mantiene pruebas legacy aisladas en ese modo y añade pruebas estrictas con sesiones y servicios efímeros.

El modelo de confianza es local: procesos que comparten usuario Unix y pueden leer todas las credenciales o escribir la base del hub no están aislados entre sí frente a acciones maliciosas. Un aislamiento más fuerte requiere permisos o procesos separados del sistema operativo.

Referencias: el encabezado y la protección de tokens Bearer siguen las convenciones descritas en [RFC 6750](https://www.rfc-editor.org/rfc/rfc6750.html); esto no constituye un servidor OAuth completo y la excepción HTTP de loopback es una decisión del despliegue local. Los clientes comparten el mecanismo de autenticación de [HTTPX](https://www.python-httpx.org/advanced/authentication/).
