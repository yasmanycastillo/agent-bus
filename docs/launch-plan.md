# Plan de primera versión pública

## Objetivo

Que una persona ajena al proyecto instale agent-bus, conecte dos agentes y
entienda un conflicto de edición y una entrega sin asistencia del autor.
La meta de dos minutos se medirá; todavía no es una garantía comercial.

Mensaje: **Coordina agentes de distintas herramientas sobre un mismo proyecto:
comparte tareas, reserva archivos y entrega resultados con trazabilidad mediante MCP.**

## Etapas y criterios de salida

| Etapa | Entrega | Criterio de salida | Estado |
|---|---|---|---|
| 1. Primer uso | `onboard --mcp-only`, configuración por identidad, licencia y guía | Instalación del wheel fuera del checkout; hub autenticado; dos conexiones MCP; repetición sin rotar credenciales; conflictos diagnosticados | Implementado y validado localmente |
| 2. Consola única | Resumen de intervención, detalle de tarea, aprobaciones completas, actividad filtrable | Prueba real de navegador: login, tarea, aprobación, desconexión y reconexión; conservar cursores y protección de sesión; luego migrar `/room` | Pendiente |
| 3. Demo | Dos agentes sobre un repositorio pequeño: reserva, conflicto, entrega y revisión | Ejecución repetible; distinguir actores simulados de modelos reales; medir duración, intervenciones y coste cuando sea conocido | Pendiente |
| 4. Preparar release | README, changelog, paquete, metadatos Registry, video de 60–90 s | Versión instalable desde máquina limpia; enlaces válidos; licencia incluida; video muestra la versión real | Pendiente |
| 5. Piloto externo | Usuarios nuevos realizan el recorrido sin ayuda | Registrar problemas y porcentaje de primeros usos completados; corregir bloqueos antes de atraer más tráfico | Pendiente externo |
| 6. Distribución | PyPI/Registry y lanzamiento acotado en comunidades | Nombre del paquete y namespace verificados; texto revisado; disponibilidad para atender problemas | Pendiente de publicación |

## Decisiones

- Evolucionar `onboard`; evitar otro asistente independiente.
- Ofrecer MCP sin arrancar workers, worktrees ni integración automática.
- Una identidad por aplicación; credenciales provisionadas por operador y sin tokens en snippets.
- Conservar configuraciones existentes; fallar con orientación ante proyecto/puerto/credencial incorrectos.
- Priorizar Linux inicialmente; documentar plataformas probadas antes de prometer compatibilidad.
- Separar demostración determinista del bus de evaluación con proveedores/modelos reales.
- Integrar lo útil del Room en la consola; retirar el duplicado solo tras comprobar paridad y recuperación.
- No anunciar autonomía continua, consenso fiable ni cuotas efectivas por la mera presencia de componentes.
- El Registry distribuye metadatos; el paquete requiere su propio canal de distribución.
- El trabajo técnico local no implica enviar mensajes a comunidades ni publicar usando cuentas externas.

## Medición de primer uso

Registrar sistema, versión Python/uv, cliente/modelo, instalación fría o con caché,
segundos hasta primer `bootstrap_agent` válido, segundos hasta entrega,
intervenciones humanas y resultado. Probar sin checkout, sin credenciales previas,
con puerto ocupado, credencial revocada y repetición del asistente.

## Próximas integraciones

Elegir según los pilotos: formatos de configuración adicionales, plugin de cliente
y notificaciones de aprobación. No añadir simultáneamente Slack, Telegram y varios
marketplaces antes de comprobar uso recurrente.

## Evidencia de la primera entrega

- 39 pruebas dirigidas aprobadas (onboarding, CLI, autenticación y resolución de
  proyecto), dos avisos existentes de deprecación WebSocket, en 8,34 s.
- Wheel construido e instalado en un entorno independiente con Python 3.13.1;
  dos clientes MCP por stdio completaron bootstrap usando los snippets generados.
- Pruebas del checkout con Python 3.12; tokens ausentes de la salida, credenciales
  conservadas al repetir, sesión revocada rechazada y puerto ajeno detectado antes
  de crear el marcador del proyecto. Licencia y consola incluidas en el wheel.
- Ruff y comprobación de espacios del diff limpios. No se repitió la suite completa.
- Esto valida entornos temporales locales, no una máquina externa limpia ni el
  comportamiento de modelos comerciales. Configuración automática por cliente,
  acceso visual guiado y medición del tiempo de primer uso siguen pendientes.
- Validación completa antes de publicar esta etapa: **731 pruebas aprobadas**,
  dos avisos de deprecación WebSocket, en 183,65 s (`uv run pytest -q`).
