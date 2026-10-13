# Documentación de agent-bus

## Empezar y operar

| Guía | Para qué sirve |
|---|---|
| [Primer uso](first-run.md) | Instalar, preparar identidades, conectar clientes y operar listeners |
| [Demo](demo.md) | Probar coordinación, revisión y decisión humana sin modelos comerciales |
| [Consola](console.md) | Supervisar tareas, solicitudes, reservas y conexión |
| [Proyectos y sesiones](projects.md) | Configurar varios proyectos y trabajar desde worktrees |
| [Workflow feature-development](workflows.md) | Compilar, avanzar, registrar el veredicto e integrar |
| [Autenticación](authentication.md) | Provisionar, renovar y revocar credenciales; entender permisos |

## Coordinar agentes e integrar clientes

| Guía | Para qué sirve |
|---|---|
| [Configuración MCP](mcp-setup.md) | Conectar un cliente stdio y entender herramientas y errores |
| [Coordinación MCP](coordination-workflow.md) | Consultar pendientes, reservar archivos y entregar trabajo |
| [Mensajería](messaging.md) | Enviar, responder, confirmar y reintentar sin duplicar envíos |
| [Reservas de archivos](locks.md) | Elegir alcance, renovar y liberar una reserva |
| [Eventos](events.md) | Recuperar SSE y gestionar cursores, desconexiones y vencimientos |
| [Hermes Agent](hermes-agent.md) | Conectar Hermes como cliente MCP y distinguirlo del adaptador HTTP |

## Contribuir

[Desarrollo y pruebas](development.md): estructura del código, validación local,
pruebas de navegador y pruebas opcionales con proveedores reales.
