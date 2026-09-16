# Demo reproducible: reserva, revisión y decisión humana

La demo utiliza **dos actores programados**, backend y QA. No llama modelos IA
ni consume suscripciones de proveedores. Los dos clientes MCP por stdio, el hub
HTTP autenticado, SQLite, las reservas y las pruebas de Python son reales.

## Ejecutar

Con agent-bus instalado:

```sh
agent-bus demo
```

Desde el checkout:

```sh
uv run agent-bus demo
```

No exige configurar un proyecto, preparar credenciales ni arrancar el hub habitual.
Crea un proyecto temporal independiente, elige un puerto local y provisiona sus
propias identidades. Ignora las variables AGENT_BUS heredadas para no usar el
runtime de otro proyecto. Al terminar cierra sus clientes, detiene su propio hub
y elimina el directorio temporal, tanto si se aprueba como si se rechaza.

## Qué ocurre

1. Backend y QA hacen `bootstrap_agent` por dos conexiones MCP independientes.
2. Cada uno reclama su tarea. Backend reserva `cart.py`.
3. QA intenta reservar `qa_notes.md` y `cart.py`: recibe 409. Se comprueba que no
   conserve ni siquiera la reserva parcial de `qa_notes.md`.
4. Backend implementa una función sencilla y ejecuta una prueba básica real.
5. Entrega a QA con evidencia, libera la reserva y repite la petición: se verifica
   que el mismo handoff no produzca un segundo mensaje.
6. QA ejecuta el caso negativo. La prueba falla porque no se lanza `ValueError`;
   responde al backend y confirma el mensaje revisado.
7. QA pide al operador autorizar la corrección. **La demo se detiene a preguntar.**
   La decisión se envía por la misma API que usa la consola y llega al inbox de QA.
8. Si se rechaza, termina sin aplicar la corrección ni completar las tareas.
9. Si se aprueba, QA comunica la autorización al backend. Backend adquiere una
   nueva reserva, corrige, ejecuta pruebas y entrega; QA repite las pruebas.
10. Ambos completan sus propias tareas; se comprueba que no queden reservas.

La intervención ocurre en la terminal. Esta versión no abre una consola temporal
ni mantiene el proyecto vivo después de la demo.

## Evidencia y automatización

```sh
agent-bus demo --report demo-evidence.json
agent-bus demo --yes --report demo-automated.json
```

`--report` no sobrescribe un archivo existente. Guarda etapas, duraciones, conteo
de llamadas MCP, decisión, estado final, código corregido y salida real de pruebas.
No exporta credenciales ni tokens de reservas. El directorio del informe debe existir.

`--yes` **simula la aprobación** y lo declara tanto en terminal como en JSON.
El contador de intervenciones humanas es cero en ese modo. Se conserva el rechazo
como resultado `declined`; los errores de infraestructura devuelven un código de
salida distinto de cero y no producen un informe de éxito.

## Alcance de esta evidencia

- Comprueba el protocolo y una implementación pequeña, no razonamiento de modelos.
- El fallo inicial es intencional: demuestra que la revisión detecta un problema
  real en el código de ejemplo. No es una tasa de fallos o éxito de un proveedor.
- La evidencia de pruebas sigue siendo declarada por los actores al enviarse al
  bus; en este recorrido el propio guion ejecuta esas pruebas y conserva su salida.
- La duración local con dependencias instaladas no mide instalación fría, descarga,
  conexión de aplicaciones externas ni tiempo de respuesta de modelos comerciales.
- `model_calls` vale cero; el coste de modelos no se presenta como una estimación.

## Siguiente prueba con proveedores reales

Repetir el escenario con dos aplicaciones/modelos conectados mediante el onboarding,
sin indicarles la implementación de la corrección. Registrar versiones, instrucciones,
resultado, consumo disponible y todas las intervenciones. Mantener esa evidencia
separada de la demo determinista antes de presentar comparaciones comerciales.
