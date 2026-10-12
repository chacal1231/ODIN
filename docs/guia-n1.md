# Guía para N1

El nivel 1 vigila la red e informa las caídas al grupo del NOC; el nivel 2 gestiona con los proveedores. Esta guía explica cómo trabajar un turno con ODIN. La meta es que, ante cualquier falla, el N1 responda en menos de un minuto:

1. **¿Es nuestro o es de afuera?**
2. **¿A quién y a cuántos afecta?**
3. **¿Qué hago ahora?**

## La bandeja

**Inicio → Bandeja** es la pantalla de trabajo. Se actualiza sola y tiene, de arriba hacia abajo:

- **Salud de ODIN**: una franja con el estado del sondeador, el tracer, SmartOLT, el analista, los lectores de eventos, el correlador y la cola. Si algo está en rojo, lo que muestra ODIN puede estar incompleto ([Vigilancia de ODIN](vigilancia-odin.md)).
- **Veredicto de servicios externos**: un aviso que aparece solo cuando hay servicios degradados. En rojo si es propio o de una salida (hay que actuar), en amarillo si es un servicio que falla por su cuenta (solo informar).
- **Entrega de turno**: la última entrega y, al abrirla, el resumen del turno en curso.
- **Necesita de ti**: lo que nadie atendió todavía.
- **En curso**: lo ya informado o reconocido.
- **Resuelto en el turno**: lo cerrado en las últimas 12 horas, con su duración y, si hubo diagnóstico, un acceso para valorarlo.

### Orden de "Necesita de ti"

1. **Crítico** antes que advertencia.
2. Dentro de cada nivel, primero lo que tiene el **plazo de informe vencido**.
3. Luego lo de **impacto confirmado** y lo que **afecta a más clientes**.
4. Por último, lo **más antiguo**.

### Cómo leer una fila

```
Acceso OLT-NORTE                                   hace 12 min   Abrir
POP Centro · Corte de fibra común: 2 PON con LOS a la vez, 50 ONUs (Centro, Norte)
👥 Confirmado: 300 ONUs afectadas de 512 de la OLT (zonas Centro, Norte)
[Sin informar · plazo vencido]  [Corresponde escalar a N2]
```

- **Título**: tipo de caso y sujeto (OLT, servicio, interfaz o sesión BGP).
- **Segunda línea**: dónde, y la **lectura de ODIN**. Responde "¿nuestro o de afuera?".
- **👥**: el **impacto**. Responde "¿a cuántos?". En rojo cuando está confirmado.
- **Marcas**: si ya se informó, si venció el plazo y si corresponde escalar.

## Las tres preguntas, según el tipo de caso

### Acceso: OLT y PON

| Lectura de ODIN | ¿Nuestro o de afuera? | Qué hacer |
|---|---|---|
| SmartOLT no llega a la OLT | Nuestro: la OLT, su energía o su uplink | Informar y escalar de inmediato |
| Uplink de la OLT caído | Nuestro: el enlace hacia el POP | Informar y escalar de inmediato |
| Corte de fibra común (LOS en varios PON a la vez) | Nuestro: un tramo compartido o la tarjeta | Informar y escalar para despacho |
| Corte de fibra en un PON | Nuestro: la distribución de ese PON | Informar; escalar si sigue |
| Falla de energía | **De afuera**: las ONUs de los clientes se quedaron sin energía | Informar como falla eléctrica; no requiere despacho |
| ONUs sin conexión | Sin causa clara | Informar y vigilar |

El impacto de acceso es **confirmado**: son las ONUs que SmartOLT reporta caídas, con sus zonas y splitters.

### Servicios externos

| Veredicto | ¿Nuestro o de afuera? | Qué hacer |
|---|---|---|
| Problema propio o del origen | Nuestro: caen servicios por varias salidas a la vez | Informar y escalar de inmediato |
| Falla en una salida | Del proveedor de esa salida (tránsito, IXP) | Informar los servicios afectados; N2 gestiona con el proveedor |
| Falla en una salida (interconexión directa) | De ese proveedor (Google, Meta…) o de la interconexión | Informar; N2 revisa la interconexión |
| Problema del servicio | **De afuera**: el servicio falla mientras otros por la misma salida están bien | Informar; no requiere N2 |
| Sin veredicto | No se sabe todavía | Vigilar |

El impacto de un servicio externo es "los clientes que usan ese servicio": ODIN no mide el uso por cliente. Cómo se decide el veredicto: [Casos y veredictos](casos-y-veredictos.md).

### Red propia: interfaces, BGP y latencia

El caso dice **qué es** el enlace, leído de la descripción del puerto: tránsito, transporte, IXP, peering, enlace interno o CDN interno. También muestra los casos relacionados del mismo equipo o POP. El impacto puede ser:

- **Potencial**: las ONUs de las OLTs que cuelgan de esa interfaz o equipo, cuando SmartOLT todavía no reporta caídas. Si la OLT tiene otro enlace, ODIN lo indica.
- **Confirmado**: cuando esas OLTs ya tienen caídas en SmartOLT.
- **Indirecto**: una salida a internet; el tráfico puede salir por las otras.

Los casos de tránsito, transporte y enlaces internos son críticos desde la primera caída.

## Informar al grupo del NOC

Al abrir un caso, la sección **Comunicación al NOC** muestra:

- **La guía de la situación**: qué verificar (2 o 3 pasos) y el criterio para escalar a N2.
- **Cuatro botones**: **Informar**, **Actualizar**, **Restablecido** y **Escalar a N2**. Cada uno propone un texto con los datos del caso (lugar, lectura, afectación, zonas, inicio y duración). Se revisa, se ajusta si hace falta y se envía.
- **El historial de informes**: quién, cuándo y cómo se entregó cada uno por Telegram y WhatsApp.

Buenas prácticas:

1. **Verificar antes de informar**: lo que pide la guía, por ejemplo "¿hay más PON de la misma zona cayendo?".
2. **Informar dentro del plazo** (10 minutos por defecto para casos críticos y de acceso). Pasado el plazo, el caso sube en la bandeja marcado como vencido.
3. **Actualizar** cuando cambia algo: crece el impacto, se suma una zona o N2 da una novedad.
4. **Escalar a N2** cuando la guía lo indica. Para escalar hay que completar el motivo.
5. **Restablecido** cuando el caso se recupera, para cerrar el ciclo en el grupo.

Un caso informado o reconocido pasa a **En curso**. Los informes también quedan como notas del caso, y el analista las tiene en cuenta.

## Lo que ODIN publica solo

Además de los informes del N1, ODIN publica en el grupo del NOC, por Telegram y WhatsApp:

- **Cada caso que avisa**, en su propio hilo. Ahí mismo, el diagnóstico del analista, el restablecimiento o una nueva caída.
- **La entrega de turno** al terminar cada turno, y la **apertura** al empezar después de horas sin turno.

Los mensajes son informes técnicos:

```
[CRÍTICO] Caída de acceso · OLT-NORTE · POP Centro
Estado: activo desde 12/10 14:10
Lectura: Corte de fibra común: 2 PON con LOS a la vez, 50 ONUs (Centro, Norte)
Afectación: Confirmado: 300 ONUs afectadas de 512 de la OLT (zonas Centro, Norte)
Ref. en ODIN
```

En Telegram, el diagnóstico trae botones para **valorarlo** (correcto, parcial o incorrecto) y para **reconocer** el caso. Si es parcial o incorrecto, ODIN pide responder con la causa encontrada: esa corrección mejora los análisis siguientes del mismo asunto. Para preguntar al analista, se responde al hilo del caso o se usa `/pregunta`.

## Entrega de turno

Los turnos se configuran por ISP (por defecto, de 06:00 a 20:00). Al cerrar cada turno, ODIN publica:

- **Casos abiertos**, con su impacto y si fueron informados y escalados.
- **Casos resueltos**, con su duración.
- **Informes del turno**, y cuántos casos quedaron sin informar o se informaron fuera de plazo.

Al empezar el turno siguiente, la **apertura** resume lo que pasó durante las horas sin turno. Conviene leerla antes de tomar la bandeja.

## Cuándo no confiar en lo que muestra ODIN

- **La salud de ODIN está en rojo**: sin sondeador, sin tracer o sin lectura de SmartOLT, ODIN no ve parte de la red. Un "normal" en ese momento no significa normal.
- **"Sin veredicto"**: falta una traza reciente o no hay otro servicio por la misma salida para comparar.
- **Impacto potencial**: es una estimación por topología, no una confirmación.
- **Todo se mide desde el servidor ODIN**: un solo punto de vista. Un veredicto o una lectura es la interpretación de las mediciones, no una causa confirmada.

## Glosario

| Término | Significado |
|---|---|
| Caso | Un problema correlacionado: una sesión BGP, una interfaz, la latencia hacia un destino o servicio, o el acceso de una OLT. |
| Incidente | La degradación de una comprobación de sondeo (pérdida, latencia, jitter o sin respuesta). Varios incidentes pueden formar un caso. |
| Lectura | La interpretación determinista de ODIN sobre los datos (por ejemplo, "corte de fibra común"). |
| Veredicto | Para servicios externos: si el problema es propio, de una salida o del servicio. |
| Salida | La red por la que el tráfico deja el ISP hacia un servicio: un tránsito, un IXP o una interconexión directa. |
| Impacto confirmado / potencial / indirecto | ONUs que SmartOLT reporta caídas / ONUs detrás de lo que falló / una salida a internet con alternativas. |
| Observación | Semana de puesta en marcha en la que ODIN calcula casos sin publicar en el grupo del NOC. |
| N1 / N2 | Nivel 1 vigila e informa; nivel 2 gestiona con proveedores y campo. |
