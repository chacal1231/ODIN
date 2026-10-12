# Comunicación del NOC

El nivel 1 vigila e informa; el nivel 2 gestiona con los proveedores. ODIN guía al N1 en cada caso (qué verificar, qué informar, cuándo escalar), registra lo que informa y lo publica en el grupo del NOC por **Telegram** y **WhatsApp**. Se configura en **Configuración → Comunicación NOC** (solo administradores).

## Canales

- **Telegram**: el grupo que un administrador vincula con `/grupo` (ver README).
- **WhatsApp**: el servicio `whatsapp` (whatsapp-web.js 1.34.7, con Chromium) se vincula escaneando el QR que muestra ODIN (WhatsApp → Dispositivos vinculados) y se elige uno de los grupos de la cuenta. La sesión se conserva en el volumen `whatsapp_data`. El servicio solo es accesible dentro de la red de Compose, con un token derivado de la clave maestra. WhatsApp Web no es una API oficial: usar un número dedicado a ODIN. Por WhatsApp solo se envía; valorar diagnósticos y consultar a ODIN se hace en ODIN o en Telegram.

Por ambos canales salen los casos que alertan (en su propio hilo: el caso, su diagnóstico, el restablecimiento o una nueva caída), los informes del N1 y las entregas de turno. Lo que se envía sale de la red.

## Tono

Los mensajes son informes técnicos, con una etiqueta de estado y campos fijos, sin emojis ni frases conversacionales ([messages.py](../backend/odin/noc/messages.py)):

```
[CRÍTICO] Caída de acceso · OLT-NORTE · POP Centro
Estado: activo desde 12/10 14:10
Lectura: Corte de fibra común: 2 PON con LOS a la vez, 50 ONUs (Centro, Norte)
Afectación: Confirmado: 300 ONUs afectadas de 512 de la OLT (zonas Centro, Norte)
Ref. en ODIN
```

Etiquetas: CRÍTICO, ADVERTENCIA, DIAGNÓSTICO ODIN, RESTABLECIDO, NUEVA CAÍDA, INFORME N1, ACTUALIZACIÓN N1, ESCALADO A N2, ENTREGA DE TURNO y APERTURA DE TURNO.

## Guías por situación

Cada caso se asocia a una situación según lo que ODIN ya lee: la lectura de acceso de la OLT, el veredicto de servicios externos o el tipo de caso de la red propia ([guides.py](../backend/odin/noc/guides.py)). Las situaciones son: OLT sin gestión, uplink de OLT caído, corte de fibra común, corte de fibra en un PON, falla de energía, ONUs sin conexión, caída propia, falla en una salida a internet, servicio externo con falla propia, servicio externo sin veredicto, interfaz caída, sesión BGP caída y latencia en un equipo propio.

Cada guía tiene qué verificar (2 o 3 pasos), una plantilla de informe y el criterio y los minutos para escalar a N2. ODIN trae guías por defecto; cada ISP las ajusta y puede volver a la versión por defecto.

## Informes del N1

En cada caso, **Informar**, **Actualizar**, **Restablecido** y **Escalar a N2** proponen un texto con los datos del caso (lugar, lectura, afectación, zonas, inicio, duración). El N1 lo revisa y lo envía. El informe queda en el caso (también como nota que lee el analista), con su autor, su hora y el resultado por canal. Escalar exige completar el motivo.

Un caso crítico o de acceso que sigue caído sin el primer informe queda **fuera de plazo** pasados los minutos configurados (10 por defecto): en la bandeja sube primero dentro de su nivel y se marca. Al cumplirse los minutos de escalamiento de su guía, se marca **Corresponde escalar a N2**. Un caso informado o reconocido pasa a **En curso**.

## Turnos

Los turnos se configuran en hora local (`ODIN_TIMEZONE`); por defecto, de 06:00 a 20:00. Al terminar un turno, ODIN publica la **entrega de turno**: casos abiertos (informados, escalados o no), resueltos con su duración e informes hechos, incluidos los fuera de plazo. Al empezar un turno después de horas sin turno, publica la **apertura** con lo ocurrido en esas horas. La bandeja muestra la última entrega y el resumen del turno en curso.
