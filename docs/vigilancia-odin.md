# Vigilancia de ODIN

ODIN evalúa su propia cadena de datos en cada consulta de `/api/v1/monitoring` (cada 15 segundos en el panel) y la publica como `odin_health`. La barra de estado muestra «ODIN operativo» o «ODIN · N avisos». Al pulsar, se despliega cada aviso con su detalle, el último error de la fuente y un acceso a la sección afectada. No requiere servicios adicionales ni envía notificaciones externas.

## Comprobaciones

| Aviso | Advertencia | Crítico | Sección |
| --- | --- | --- | --- |
| Sondeador sin señal | — | Heartbeat con más de 30 s | Red propia |
| Sondeador atrasado | Menos del 90 % de las rondas programadas | Menos del 50 % | Red propia |
| Lectura detenida (por fuente y flujo) | Sin lectura exitosa durante más de 5 min (o 10 intervalos) | Más de 30 min | Eventos |
| Cola de trabajos atrasada | Trabajo pendiente vencido hace más de 5 min | Más de 15 min | Eventos |

- **Ritmo de rondas.** Compara los lotes programados terminados (correctos o fallidos) con los turnos esperados en una ventana de 9 minutos, que termina 1 minuto antes de la evaluación para no contar como faltantes las rondas en curso. Se excluyen los destinos pausados, los archivados y los creados o modificados dentro de la ventana. Una ronda fallida cuenta como ejecutada: el aviso mide si el sondeador cumple su agenda, no el estado de los destinos. Si el sondeador no tiene señal, solo se informa ese aviso.
- **Lectores de eventos.** Solo se evalúan las fuentes con lectura activada; pausar una fuente retira su aviso. Una fuente que nunca leyó con éxito se mide desde su última configuración. El aviso incluye el último código de error, por ejemplo `revision_conflict`, y el número de fallos consecutivos.
- Con el sondeador críticamente atrasado, el estado general de la barra pasa a «Sin datos fiables» aunque los destinos conserven su último estado.

## Validación

- Pruebas en `backend/tests/test_health.py`, que cubren cada umbral, los destinos recién modificados, las fuentes en pausa, las que nunca leyeron y la inclusión en `/monitoring`. Suite general: 157 pruebas aprobadas.
- La interfaz compila con TypeScript/Vite. No se hizo una revisión visual con avisos activos en el navegador.

## Limitaciones

- Los avisos solo se ven con el panel abierto; no hay notificaciones externas.
- No se mide el consumo de recursos del host ni de PostgreSQL. El atraso del sondeador es el síntoma operativo que se vigila.
- La evaluación se ejecuta en la API: si la API o la base de datos caen, el panel muestra «Sin conexión con ODIN», no el detalle.
