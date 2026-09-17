# Reactivación automática de Grok: piloto verificado

El 17 de septiembre de 2026 (UTC), una solicitud enviada por Codex al bus
inició automáticamente un turno headless real de Grok 4.6. El watcher publicó
`DESPIERTO-8467` y confirmó la solicitud en **9,608 segundos**. El listener ya
estaba activo antes del envío; el operador no inició Grok después del mensaje.

Después de reiniciar el listener y observar más de diez segundos, seguía
existiendo una sola respuesta. La solicitud permanecía confirmada y el mapa
de sesiones persistía. La [evidencia sanitizada](evidence/grok-autowake.json)
incluye identificadores y tiempos del intercambio. El estado de la respuesta
en ese archivo corresponde a la captura anterior a su ACK por Codex.

## Configuración reproducible

Requisitos: Grok CLI autenticado, identidad agent-bus provisionada, entorno del
proyecto configurado y acceso al hub. El piloto usó Grok CLI 1.0.34 y Grok 4.6.
No copiar credenciales entre identidades. Ejecutar desde el proyecto deseado:

```bash
agent-bus watch --agent <identidad-grok> \
  --cli /ruta/agent-bus/examples/grok-watch-cli.sh
```

El [lanzador](../examples/grok-watch-cli.sh) conserva los argumentos del watcher
y usa el directorio actual, sin rutas temporales incrustadas. Se puede cambiar
el modelo mediante `GROK_WATCH_MODEL`; otros modelos no fueron validados aquí.
Los permisos MCP del ejemplo se limitan al nombre `agent-bus-pilot`; adaptar
esa lista si el servidor tiene otro nombre. La prueba de despertar no requirió
herramientas del modelo: el watcher gestionó la recepción, respuesta y ACK.

Enviar a esa identidad una solicitud con `reply_needed=true`. El watcher
ejecuta el CLI, publica el texto final y confirma el mensaje después del éxito.
Las notificaciones sin respuesta requerida no disparan este recorrido.

## Incidencia corregida e intervenciones

El primer lanzador añadía `--output-format json`, que el watcher también
proporciona. Grok rechazó el argumento duplicado. Hubo cinco intentos fallidos,
sin confirmación automática. La variante publicada deja ese argumento al
watcher. Se envió una nueva solicitud, que completó el ciclo, y el operador
archivó manualmente el intento anterior por estar sustituido.

El operador preparó credenciales, arrancó el listener, envió la solicitud y
reinició el listener para la comprobación. El arranque del turno de Grok y la
respuesta a la solicitud exitosa fueron automáticos. El piloto se ejecutó en
un proyecto temporal y hub aislados; no se modificó código de agent-bus.

## Alcance

- Se inicia un proceso headless; no se inyecta un turno en una TUI existente.
- Un heartbeat indica presencia, pero no sustituye al listener ejecutor.
- Se comprobó un reinicio posterior al ACK, no una caída durante la respuesta.
- Una ejecución no acredita ausencia universal de duplicados ni compatibilidad
  con cualquier cliente o modelo.
- El listener debe seguir ejecutándose; esta prueba no instala un servicio que
  sobreviva al reinicio del equipo.
