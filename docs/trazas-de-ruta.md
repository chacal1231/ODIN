# Trazas de ruta

ODIN traza la ruta hacia cada comprobación con `mtr` (10 ciclos, numérico, ASN por salto vía Team Cymru) desde el servidor ODIN. Corre en su propio servicio, `tracer`, con el mismo endurecimiento que `probe` (solo `NET_RAW`, sistema de archivos de solo lectura), para no retrasar las rondas de sondeo.

## Cuándo se traza

- **Línea base** cada 15 minutos por comprobación activa. Un turno que se atrasa más de dos minutos no se recupera; si queda pendiente más de 15 minutos se cancela y lo reemplaza el siguiente.
- **Apertura y recuperación de incidente**: la ruta durante la falla y después, vinculadas al incidente. Tienen prioridad sobre las líneas base.
- **Manual**, desde la ficha del destino. Disponible para operadores y administradores, con límite de 10 cada 5 minutos por usuario.

Se traza siempre una dirección que la comprobación ya mide (aprobada o resuelta); nunca un nombre DNS. Las comprobaciones TCP/HTTPS se trazan con TCP al mismo puerto.

## Lectura de cada salto

| Segmento | Significado |
|---|---|
| Red propia | La IP es de un puerto o de un equipo del inventario. |
| Frontera | La IP está en la subred de un puerto propio sin ser nuestra: es el otro extremo de ese enlace. Se muestra el rol según la descripción del puerto (`TRANSITO-…`, `PIT-…`, `PNI-…`); ver `inventory/roles.py`. |
| Externa / Privada | IP pública o privada fuera del inventario. |
| Destino | La dirección medida. |
| Sin respuesta | El router no respondió. Es normal. |

Sin inventario de LibreNMS los saltos propios aparecen como externos o privados.

El resumen indica por qué enlace sale la ruta y la secuencia de sistemas autónomos. La huella de la ruta conserva equipos, enlaces de frontera y AS, no cada IP, de modo que el balanceo ECMP dentro de una misma red no cuenta como cambio. Se marca **Cambio de ruta** solo entre trazas que llegaron al destino.

## Límites

- Un solo punto de vista: no se ve la ruta de regreso.
- El ASN es el origen del prefijo según Team Cymru, no necesariamente quien opera el router.
- El núcleo MPLS de un proveedor sin propagación de TTL aparece como menos saltos.
- Pérdida en un salto intermedio que no continúa hasta el destino suele ser limitación de ICMP del router.

## Retención

Líneas base: 3 días; las que registraron cambio de ruta y las manuales, 90 días; las de incidente, mientras exista el incidente (366 días). Si el trazador deja de enviar señal aparece **Trazas detenidas** en la barra de estado.

## Trazas incoherentes y concurrencia

Una traza en la que la misma IP aparece en saltos no contiguos se descarta con `trace_inconsistent`: son respuestas atribuidas a otra sonda (o un bucle de enrutamiento) y compararla generaría cambios de ruta falsos. Varios mtr simultáneos en el mismo contenedor se atribuyen respuestas entre sí, con ICMP y con TCP: con 8 a la vez se mezclaba el 20 % de las trazas, y con 4 también ocurrió. Por eso `ODIN_TRACE_CONCURRENCY` vale 1: cada traza dura unos 7,5 s y una sola cola cubre unas 110 comprobaciones en cada ciclo de 15 minutos; por encima, las trazas de base atrasadas se descartan y se reemplazan por las siguientes. Subir la concurrencia vuelve a mezclar respuestas. El tracer y el sondeador corren con `init: true`: mtr deja procesos hijos que, sin un init que los recoja, se acumulan como zombis hasta agotar el límite de procesos del contenedor.
