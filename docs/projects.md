# Proyectos y sesiones

Cada proyecto tiene un hub y una base SQLite propios. El identificador persistido en `project_metadata` vincula toda la base a ese proyecto: arrancar o provisionar con otro identificador falla. No hay un servidor multitenant con tablas compartidas. Dos proyectos simultáneos necesitan URL/puertos distintos; no se asignan puertos automáticamente.

## Configurar

Desde cada repositorio, con el ejecutable instalado:

```bash
agent-bus init --bus-url http://127.0.0.1:8421
agent-bus auth create --agent human --role admin
agent-bus auth create --provider claude
agent-bus auth create --provider claude
agent-bus serve --daemon
```

Cada `--provider` genera una identidad y un archivo de credenciales diferentes; conservar las rutas impresas. `--agent <id>` permite elegir una identidad estable. Crear otra credencial para ese mismo ID sigue representando al mismo participante. El proveedor es metadato, no la identidad operativa.

Desde otra ubicación: `agent-bus --project /ruta/repositorio work check`. También se puede fijar `AGENT_BUS_PROJECT_ROOT` a una ubicación absoluta existente. Si se ejecuta mediante `uv --project /ruta/instalacion`, esa opción selecciona el paquete; `AGENT_BUS_PROJECT_ROOT` selecciona el proyecto coordinado.

## Resolución

La búsqueda asciende desde el directorio de trabajo. En Git, los worktrees comparten la configuración de la ubicación canónica del checkout principal; un repositorio Git anidado constituye un límite independiente. Los archivos de protocolo generados se escriben en el checkout de trabajo. El ID inicial deriva de la ruta canónica; `init` lo conserva en `.agent-bus/config.yaml`. Mover un proyecto inicializado conserva ese ID. Copiar su configuración representa deliberadamente el mismo namespace: inicializar una configuración nueva para crear otro proyecto.

El runtime predeterminado es `.agent-bus/runtime/`: base, credenciales, identidad seleccionada, contexto, PID/logs y estado de workers/watchers. Sin proyecto descubierto se conserva el directorio externo `~/.agent-bus`. `AGENT_BUS_CONFIG_DIR` selecciona un runtime independiente; sin `AGENT_BUS_PROJECT_ROOT` no hereda el proyecto del cwd.

| Valor | Precedencia |
| --- | --- |
| URL | argumento, `AGENT_BUS_URL`, `bus_url` del proyecto, host/puerto del runtime, loopback:8420 |
| ID | `AGENT_BUS_PROJECT_ID`, `bus.project_id` del runtime, ID guardado o derivado del proyecto, `default` externo |
| Base | `AGENT_BUS_DATABASE_PATH`, `database_path` del runtime, directorio de datos |

Las rutas relativas de YAML se resuelven desde ese archivo; las de entorno, desde el cwd de invocación. Los subprocesos reciben rutas absolutas, proyecto y URL fijados. MCP captura proyecto, URL y credencial al construirse. `AGENT_BUS_SESSION_FILE` selecciona su identidad si no hay un ID explícito; un ID explícito incompatible se rechaza.

`quickstart` comprueba el proyecto del hub y sólo autoinicia un origen HTTP loopback, usando su puerto configurado. Un hub remoto/HTTPS debe arrancarse explícitamente. Los clientes envían `X-Agent-Bus-Project`; un destino de otro proyecto devuelve 403. La autorización sigue dependiendo de la sesión Bearer, no de esa cabecera.

## Consumidores y migración

Varias conexiones MCP con la misma credencial comparten permisos e inbox. Leer no reserva ni confirma una entrega; deben coordinar quién procesa y confirma. Para trabajo independiente, usar identidades distintas.

Los ejecutores automáticos `worker` y `watch` adquieren un `flock` local por base canónica, proyecto y agente. Sólo uno ejecuta; observadores `watch --dry-run` pueden coexistir. El guard se libera al terminar o morir el proceso y su archivo no se elimina. Esto requiere Unix y almacenamiento local compartido; no constituye una lease distribuida ni garantiza efectos externos exactamente una vez. T-12 incorpora [leases de edición por sesión](locks.md), con alcance físico de checkout o lógico compartido del proyecto.

Una base heredada sin marcador se adopta una sola vez, si sus sesiones existentes pertenecen al proyecto solicitado. Las bases con sesiones de varios proyectos se rechazan; no se separan automáticamente. Para mantener un despliegue antiguo, fijar su directorio, base e ID existentes antes de reiniciarlo. Respaldar y trasladar explícitamente el antiguo `.agent-bus/context.yaml` a `<runtime>/context.yaml` si se desea conservarlo; el hub ya no lo busca por cwd. Actualizar código no reinicia ni migra un hub activo.
