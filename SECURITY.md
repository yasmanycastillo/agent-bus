# Política de seguridad

## Versiones soportadas

El proyecto está en fase alpha. Sólo la rama `main` recibe correcciones de seguridad;
no se mantienen ramas de versiones anteriores. Linux con Python 3.12 es la plataforma
validada.

## Reportar una vulnerabilidad

No publiques vulnerabilidades, credenciales ni datos de una base real en un issue.
Envía un informe privado a `yasmany003@gmail.com` con:

- versión o commit afectado;
- impacto y condiciones necesarias;
- reproducción mínima sin secretos ni datos personales;
- mitigación conocida, si existe.

Se acusará recibo cuando el mantenedor pueda revisar el informe. No se promete un SLA
mientras el proyecto sea alpha. Una corrección se coordinará de forma privada y se
publicará con una entrada en `CHANGELOG.md` cuando sea apropiado.

## Modelo de seguridad

Agent Bus coordina procesos locales que comparten un usuario y un proyecto. Las
credenciales identifican agentes dentro de ese proyecto; no convierten el hub en un
servicio apto para Internet ni aíslan procesos maliciosos ejecutados con el mismo UID.

- No expongas el hub directamente a redes no confiables.
- Protege los archivos de credenciales y la base SQLite del proyecto.
- Usa una identidad distinta por ejecutor automático.
- Las reservas son cooperativas y no reemplazan permisos del sistema operativo.
- Revisa y prueba el commit exacto antes de integrarlo; un handoff no ejecuta ni
  fusiona código por sí solo.
