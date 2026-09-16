# Consola local de supervisión

`/console` y `/room` sirven la misma aplicación. Se conserva `/room` para enlaces
existentes y `/room/api/*` para compatibilidad; el HTML antiguo del Room se retiró.
Los datos y las acciones requieren una sesión administrativa.

## Recorrido

1. Abrir `agent-bus ui` y entrar con la credencial administrativa del proyecto.
   El token se conserva solo en memoria, nunca en almacenamiento del navegador.
2. Consultar el resumen de solicitudes, tareas bloqueadas y revisiones pendientes.
3. Desplegar una solicitud para leer su contenido completo, elegir decisión y
   escribir una observación. Responder exige texto; cerrar el desplegable no envía nada.
4. Crear o reasignar tareas. **Ver detalle** abre un panel con descripción,
   criterios, dependencias, comando configurado y mensajes conservados.
5. Consultar actividad por agente, tarea o texto; las reservas muestran propietario
   y vencimiento. La pausa afecta a nuevas ejecuciones de workers.

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

Pendiente: vista de worktrees/commits integrados, consumo real y pilotos con modelos
externos. Esta entrega no completa todas las capacidades de T-21/T-20.
