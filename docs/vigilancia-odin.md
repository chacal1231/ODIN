# Vigilancia de ODIN

Un NOC que confía en ODIN tiene que saber cuándo ODIN deja de ver. ODIN evalúa su propia cadena de datos en cada consulta del panel (cada 15 segundos) y la muestra en dos lugares:

- **La barra de estado** dice «ODIN operativo» o «ODIN · N avisos». Al pulsarla, cada aviso muestra su detalle, el último error de la fuente y un acceso a la sección afectada.
- **La franja de salud de la bandeja** agrupa los avisos por componente: sondeador, trazador, SmartOLT, analista, lectores de eventos, correlador y cola de trabajos.

## Comprobaciones

| Aviso | Advertencia | Crítico | Qué deja de funcionar |
|---|---|---|---|
| Sondeador sin señal | — | Sin heartbeat por más de 30 s | Toda la medición propia; los estados quedan sin datos |
| Sondeador atrasado | Menos del 90 % de las rondas programadas | Menos del 50 % | Mediciones e incidentes incompletos |
| Trazas detenidas | Sin señal del trazador por más de 90 s | — | Trazas nuevas, cambios de ruta y salidas del veredicto; las mediciones siguen |
| Lectura de SmartOLT detenida | Sin leer caídas de PON por más de 20 min | Más de 60 min | Casos de acceso e impacto confirmado |
| SmartOLT limitó las llamadas | La API respondió 429 (hasta su `Retry-After`) | — | Lecturas de SmartOLT en pausa |
| Presupuesto de SmartOLT casi agotado | Queda menos del 10 % de las llamadas de la hora | — | Riesgo de 429 |
| Lectura detenida (por fuente y flujo) | Sin lectura exitosa por más de 5 min (o 10 intervalos) | Más de 30 min | Eventos de esa fuente en los casos |
| Correlador detenido | Sin una pasada correcta en más de 10 min | Más de 30 min | Los casos no se actualizan |
| Analista detenido | Sin señal del proceso por más de 2 min | — | Los análisis y el chat quedan en cola; casos y lecturas siguen |
| Análisis demorados / El modelo no responde | Análisis en cola por más de 15 min | — | Diagnósticos |
| Cola de trabajos atrasada | Un trabajo vencido hace más de 5 min | Más de 15 min | Inventario, eventos, SmartOLT, correlación y umbrales |

Detalles:

- **Ritmo de rondas.** Compara los lotes programados terminados (correctos o fallidos) con los turnos de medición esperados en una ventana de 9 minutos, que termina 1 minuto antes de la evaluación. Se excluyen los destinos pausados, los archivados y los modificados dentro de la ventana. Una ronda fallida cuenta como ejecutada: el aviso mide si el sondeador cumple su agenda, no el estado de los destinos.
- **Sondeador sin señal.** Cuando no hay señal, solo se informa ese aviso. Con el sondeador críticamente atrasado, el estado general pasa a «Sin datos fiables», aunque los destinos conserven su último estado.
- **Lectores de eventos.** Solo se evalúan las fuentes con lectura activada: pausar una fuente retira su aviso. Una fuente que nunca leyó con éxito se mide desde su última configuración. El aviso incluye el último código de error y el número de fallos seguidos.

## Lo que no se vigila aquí

- **Telegram y WhatsApp**: su último error se ve en **Comunicación NOC**, y en cada informe se ve el estado de entrega por canal.
- **Recursos del host y de PostgreSQL**: no se miden. El atraso del sondeador es el síntoma operativo que se vigila.
- **La propia API**: los avisos se calculan en ella. Si caen la API o la base, el panel muestra «Sin conexión con ODIN», sin detalle.
- Los avisos de salud no se envían al grupo del NOC: se ven con el panel abierto.

Para revisar a mano:

```bash
docker compose ps
docker compose logs --tail=50 api worker probe tracer telegram whatsapp analyst
```
