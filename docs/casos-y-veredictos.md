# Casos y veredictos

El **correlador** corre cada minuto y convierte cambios de estado en **casos**. Sus reglas son deterministas: no usan el LLM. Cada caso trae su evidencia y explica por qué está vinculada. Además, ODIN le agrega tres cosas pensadas para el nivel 1: una **lectura** o un **veredicto** (¿es nuestro o de afuera?), el **impacto** (¿a quién y a cuántos?) y una **guía** (¿qué hago?, en [Comunicación del NOC](comunicacion-noc.md)).

## Tipos de caso

| Tipo | Se abre cuando | Una entidad por caso |
|---|---|---|
| **Sesión BGP** | El log del router muestra que una sesión deja *Established* (Graylog) | Equipo + vecino |
| **Interfaz** | El log del router muestra una interfaz caída | Equipo + interfaz |
| **Latencia** | Un incidente de sondeo (pérdida, latencia, jitter, sin respuesta) | Red propia: un caso por destino. Servicio externo: un caso por servicio, con todas sus regiones |
| **Acceso** | Una OLT tiene caídas de PON, un uplink caído o SmartOLT no la alcanza | Una OLT |

### Ciclo de vida

- Los cambios del mismo equipo y sujeto se suman al mismo caso.
- **Cierre:** a los 30 minutos estable, el caso se cierra; una caída posterior abre un caso nuevo.
- **Sin actividad:** una sesión o interfaz que sigue caída sin cambios durante un día pasa a *sin actividad*. Sigue listada en Casos, pero deja de avisar.
- Los casos de latencia y de acceso nunca quedan sin actividad mientras su fuente siga reportando la falla.
- **Severidad crítica** en sesiones e interfaces: caída sostenida 15 minutos, 5 o más caídas en el caso, o, desde la primera caída, si el vecino o la interfaz es un **tránsito, un transporte o un enlace interno** (leído de la descripción del puerto; ver [Puesta en marcha](puesta-en-marcha.md)).

### Evidencia

La coincidencia en el tiempo nunca basta para vincular algo a un caso: cada evidencia comparte el equipo o el sujeto.

- **Logs** del mismo vecino o interfaz, cambios de configuración y logs críticos del mismo equipo. Si hay muchos, se guardan los primeros y los últimos, y se anota el total.
- **Alertas** de Zabbix o LibreNMS del mismo equipo o interfaz.
- **Incidentes de sondeo** de destinos asociados al mismo equipo.
- **Trazas**: en un caso de latencia, la traza tomada al abrirse y al recuperarse cada incidente, y cada cambio de ruta del período ("antes Tv&Mas → EdgeUno → Riot · ahora Tv&Mas → Cogent → Riot"). En un caso de router, los cambios de ruta de destinos cuyo camino pasa por ese equipo.
- **SmartOLT**: cada caída de PON, uplink y cambio de alcance de la OLT, con su hora.

Los **casos relacionados** son los del mismo equipo, del mismo POP, del otro extremo de la misma sesión, la interfaz del router que llega a una OLT, o la OLT que cuelga de una interfaz. Un servicio externo no comparte equipo con la red: sus relacionados solo coinciden en el tiempo, y así se indica.

## Veredicto de servicios externos

El veredicto responde, para los servicios externos, si el problema es nuestro, de una salida o del servicio ([verdict.py](../backend/odin/probes/verdict.py)).

**La salida.** Cada servicio sale de la red por una **salida**, que se lee de su última traza coherente:

- Si el inventario la conoce, es el puerto de hand-off ("sale por PE-A, tránsito CARRIER").
- Si no, es la **primera organización** que aparece después de la propia en la traza. La red propia se reconoce por `ODIN_ASN` o, si no está definido, por el sistema autónomo en el que empiezan la mayoría de las trazas.
- Las salidas se agrupan por **organización**, no por sistema autónomo: dos ASN de un mismo proveedor (por ejemplo, EdgeUno 7195 y 273993) son una sola salida.
- Si la salida es la organización del propio destino, es una **interconexión directa**.

Luego se comparan los servicios degradados con los sanos:

| Veredicto | Regla | Acción para N1 |
|---|---|---|
| **Propio o del origen** | Dos o más salidas con la mayoría de sus servicios degradados a la vez, o la mitad de todos los servicios (al menos 3) | Informar caída propia; la atiende N2 de red |
| **Falla en una salida** | Al menos 2 servicios degradados, y la mitad de los que salen por ella, mientras las demás salidas están bien | Informar los servicios afectados; N2 gestiona con el proveedor |
| **Falla en una salida (interconexión directa)** | Igual que el anterior, cuando todos los servicios de la salida son de la misma red (Google, Meta…) | Informar; N2 revisa la interconexión y, si está bien, gestiona con ese proveedor |
| **Problema del servicio** | Un servicio degrada mientras otros por su misma salida están normales | Informar que el servicio falla por sí mismo; no requiere N2 |
| **Sin veredicto** | No hay traza en las últimas 2 horas, o no hay otro servicio por la misma salida para comparar | Vigilar |

Si un servicio degradado cambió de salida entre sus dos últimas trazas, se indica junto al veredicto. Las trazas con respuestas mezcladas no se leen.

### Nombres de las redes

Los nombres y la organización de cada sistema autónomo se consultan a Team Cymru por DNS (`AS<n>.asn.cymru.com`). El tracer consulta los que aparecen en sus trazas, hasta 10 por traza, y los renueva cada 30 días. La organización es la primera palabra significativa del nombre registrado, así que dos empresas distintas con la misma primera palabra se unirían. Las redes más comunes tienen un nombre fijo de presentación.

## Casos de acceso (OLT y PON)

Un caso por OLT reúne sus caídas de PON, sus uplinks caídos y si SmartOLT la alcanza ([access.py](../backend/odin/cases/access.py)). Se recupera cuando no queda nada de eso. Las caídas de más de 7 días que SmartOLT marca como *stale* quedan fuera, porque ya son conocidas.

La **lectura** sigue este orden: lo primero que se cumple es lo que se informa.

| Situación | Lectura | Crítico |
|---|---|---|
| SmartOLT no alcanza la OLT | La OLT, su energía o su uplink ("posible caída total" si además hay PON caídos) | Sí |
| Uplink caído | La causa probable de lo que muestran sus PON | Sí |
| LOS en 2 o más PON que empiezan con menos de 10 minutos de diferencia | Corte común: un tramo compartido o la tarjeta de la OLT | Sí |
| LOS en un PON | Corte en la distribución de ese PON, con su zona y splitters | Desde 64 ONUs |
| Falla de energía (*power*) | Las ONUs de los clientes perdieron energía (*dying gasp*): no es la red | Desde 64 ONUs |
| Sin conexión (*offline*) | ONUs sin conexión, sin causa que SmartOLT pueda identificar | Desde 64 ONUs |

La entidad del caso es el equipo de la OLT en el inventario, si existe. Eso le suma las alertas de Zabbix y LibreNMS de la OLT, los incidentes de sondeo de su gestión y, como casos relacionados, la interfaz del router que llega a ella. El caso se mantiene ligado a la OLT de SmartOLT aunque la asociación con el inventario llegue después.

## Impacto en clientes

Cada caso abierto indica a cuántos clientes afecta y dónde ([impact.py](../backend/odin/cases/impact.py)):

- **Clientes = ONUs de SmartOLT**, por OLT y por zona. El impacto es general (por PON, OLT y zona), no por cliente.
- **Ubicación de cada OLT en la red:** el inventario indica qué puertos de router la alcanzan, es decir, aquellos cuya subred contiene una dirección de la OLT.

| Impacto | Cuándo |
|---|---|
| **Confirmado** | SmartOLT reporta ONUs caídas: el propio caso de acceso, o una OLT detrás del equipo o interfaz que tiene su caso de acceso abierto |
| **Potencial** | Cae una interfaz o un router con OLTs detrás, sin caídas en SmartOLT. Si la OLT sigue con otro enlace, se indica. En una sesión BGP sobre un enlace interno, cuentan las OLTs del router del otro extremo |
| **Indirecto** | Una salida a internet (tránsito, IXP, peering) o una caché interna de CDN: el tráfico tiene alternativas |
| **Servicio** | Un servicio externo: sus usuarios. Las ONUs en línea son solo una referencia, porque ODIN no mide el uso por cliente |
| **Sin datos** | Faltan SmartOLT o el inventario: el caso lo dice en lugar de estimar |

Si la gestión de las OLTs va por una red que no está en ningún puerto de router de LibreNMS, ODIN no puede saber de qué interfaz cuelgan.

## Qué avisa

Un caso **avisa** (en la bandeja, en el grupo del NOC y con análisis automático) cuando está activo y cumple alguna de estas condiciones:

| Grupo | Condición |
|---|---|
| Tránsito, transporte o enlace interno | Sesión o interfaz de ese tipo, desde la primera caída |
| Otras caídas sostenidas | Sesión o interfaz crítica por duración o por repetición |
| Latencia crítica de red propia | Caso de latencia crítico hacia un equipo propio |
| Servicios externos | Veredicto propio o de una salida. Sin veredicto, 3 o más servicios degradados a la vez. Un servicio que falla solo no avisa |
| Caídas de acceso críticas | OLT inalcanzable, uplink caído, corte común o 64 ONUs o más |

Las demás caídas de acceso no avisan por Telegram ni WhatsApp, pero aparecen en la bandeja, porque el N1 las informa todas.

**Puesta en marcha en sombra:** el interruptor **Avisos de casos** en **Casos** controla el contador de casos en la barra de estado; la bandeja, el análisis automático y los envíos a Telegram y WhatsApp no dependen de él. Para observar una semana sin publicar, vincular los grupos del NOC recién al terminarla, o tenerlos en pausa (`/pausar` en Telegram, **Pausar** en Comunicación NOC). En **Casos** también se ve la calibración de los últimos 7 días: cuántos casos habría avisado cada grupo, los asuntos más ruidosos que no avisan y las valoraciones del NOC.

## Analista

Para cada caso que avisa, el analista recibe un paquete etiquetado:

- **Encabezado:** qué es el caso, la identidad del equipo, qué es el vecino o la interfaz, la lectura o el veredicto de ODIN y el impacto.
- **Evidencia:** agrupada, y repetida una sola vez con su cuenta.
- **Contexto del NOC:** notas e informes del NOC, casos relacionados y correcciones de casos anteriores.

Responde con un resumen, hipótesis con su confianza y comprobaciones, citando las etiquetas de la evidencia. Si cita algo que no existe, la respuesta se repara una vez o se descarta. Su diagnóstico se publica en el hilo del caso.
