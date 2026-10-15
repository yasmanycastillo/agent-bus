# Consola local de supervisión

`/console` y `/room` sirven la misma aplicación con diseño **Cyber-Ops Terminal Cockpit**
en tres zonas operativas (`01 // TERMINALES DE AGENTES EN VIVO`, `02 // COLA DE INTERVENCIÓN & MATRIZ DE TAREAS`,
`03 // STREAM SSE, RESERVAS & BROADCAST`). Se conserva `/room` para enlaces existentes y
`/room/api/*` para compatibilidad; el HTML antiguo del Room se retiró. Los datos y las
acciones requieren una sesión administrativa.

## Recorrido

1. Abrir `agent-bus ui` y entrar con la credencial administrativa del proyecto.
   El token se guarda en `sessionStorage`: sobrevive al refresco, se borra al
   cerrar la pestaña o pulsar **Salir** y no se comparte con otras pestañas.
2. En la **Zona 01 (Terminales en vivo)**, inspeccionar la salida TUI de cada agente
   cada 2 s, su estado (`trabajando`, `esperando`, `inactivo con mensajes`, `watcher activo`)
   y enviarle mensajes directos desde el pie de cada celda. El panel de **Agentes y ejecución**
   muestra el estado de su credencial (`credencial ok`, `credencial vencida`, `credencial revocada`)
   para detectar de inmediato si un agente quedó en silencio por vencimiento de sesión
   (renovable con `agent-bus auth renew --agent <id> --notify`).
3. En la **Zona 02 (Intervención y Matriz de tareas)**, desplegar solicitudes pendientes
   para leer su contenido completo, elegir decisión (`Aprobar`, `Rechazar`, `Responder`) y
   escribir una observación. Responder exige texto; cerrar el desplegable no envía nada.
4. Alternar entre **`[TABLA COMPACTA]`** (por defecto, sin truncar títulos ni identificadores)
   y **`[KANBAN 5-COL]`**, crear o reasignar tareas y detectar alertas de motivo de bloqueo
   (`blocked_reason`), revisión detenida ($\ge 30\text{ min}$) o reclamo lento ($\ge 60\text{ min}$).
   **Ver detalle** abre un diálogo con descripción, criterios, dependencias, comando configurado
   y mensajes conservados.
5. En la **Zona 03 (Stream SSE, Reservas & Broadcast)**, filtrar actividad en tiempo real por
   agente, tarea o texto; auditar reservas activas con propietario y vencimiento; y consultar
   el resumen de turnos CLI registrados en `/usage/summary`.

El historial de una tarea incluye entregas ya confirmadas mientras los mensajes
sigan conservados. Se pagina en grupos de 50; leerlo no envía ACK. La evidencia
que un agente declara no se presenta como una prueba ejecutada por la consola.
La lista de actividad contiene los últimos 200 eventos recibidos en esa sesión;
no es un archivo histórico completo del proyecto.

## Conexión y sesión

- El estado inicial se carga al entrar. Se refresca tras eventos y periódicamente.
- Los eventos recuperan su cursor tras un corte. Un frame incompleto no avanza
  la posición; un cursor vencido exige recuperar el estado antes de adoptar otro.
- Pasar a modo sin conexión cancela la lectura activa; reconectar recupera mensajes.
- Salir cancela el stream y vacía los datos. Las respuestas HTTP de una sesión
  anterior se descartan, incluso si llega una nueva sesión mientras estaban pendientes.
- Una credencial rechazada cierra la sesión; un fallo de actualización muestra un
  error visible para evitar presentar datos antiguos como si fueran recientes.

## Prueba reproducible en navegador

La prueba crea un proyecto y un hub temporales, provisiona sus propias sesiones,
usa Chromium real y destruye ese entorno al terminar. No usa el hub del desarrollador.
Requiere Playwright para Node y Chromium instalado:

```sh
PLAYWRIGHT_MODULE=/ruta/node_modules/playwright \
CHROMIUM_PATH=/usr/bin/chromium \
node tests/console_browser.cjs
```

Por defecto usa `.venv/bin/python`; puede sustituirse mediante `AGENT_BUS_TEST_PYTHON`.
Genera capturas de escritorio, móvil y detalle en `/tmp/agent-bus-console-*.png`.
Cubre rol admin, más de cinco solicitudes completas, creación y reasignación,
historial sin ACK, respuesta, corte/replay, filtros, pantalla de 390 px, salida y
respuesta tardía de una sesión anterior. Los actores son de prueba, no modelos IA.

La consola todavía no muestra worktrees ni commits integrados, y el contrato
`/room/api/usage` permanece congelado mientras se consolida el presupuesto por proyecto.
Cuando el worker registra turnos CLI en `/usage`, el panel **Presupuesto y consumo**
muestra el resumen acumulado de `/usage/summary` (turnos registrados, costo estimado USD,
tokens cacheados y agente con mayor consumo). Es solo informativo: ningún tope detiene una tarea.
