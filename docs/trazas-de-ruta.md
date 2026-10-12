# Trazas de ruta

ODIN traza la ruta hacia cada comprobación con `mtr` (10 ciclos, numérico, ASN por salto vía Team Cymru) desde el servidor ODIN. Corre en su propio servicio, `tracer`, con el mismo endurecimiento que `probe` (solo `NET_RAW`, sistema de archivos de solo lectura), para no retrasar las rondas de sondeo. Las trazas sirven para tres cosas: ver por dónde sale cada destino, detectar cambios de ruta y dar la **salida** con la que se decide el veredicto de los servicios externos ([Casos y veredictos](casos-y-veredictos.md)).

## Cuándo se traza

- **Línea base** cada 15 minutos por comprobación activa. Una traza de base que se atrasa más de dos minutos no se recupera; si queda pendiente más de 15 minutos se cancela y la reemplaza la siguiente.
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

El resumen indica por qué enlace sale la ruta y la secuencia de sistemas autónomos. La huella de la ruta conserva equipos, enlaces de frontera y AS, no cada IP, de modo que el balanceo ECMP dentro de una misma red no cuenta como cambio. Se marca **Cambio de ruta** entre trazas comparables:

- las que **llegaron al destino**, y
- para destinos de catálogo con ASN de referencia, las que **llegaron a la red del destino**: no alcanzan el servidor (que no responde, o está caído durante un incidente), pero el último salto con respuesta está dentro del ASN del destino. Así un servidor de juego que no responde ICMP también muestra si cambió la entrada a su red.

## Nombres de las redes

Los ASN de cada salto se traducen a nombres y organizaciones con Team Cymru por DNS (`AS<n>.asn.cymru.com`). El tracer consulta los que aparecen en sus trazas, hasta 10 por traza, renovados cada 30 días, y los guarda en `autonomous_systems`. Una organización une los sistemas autónomos de una misma empresa (por ejemplo, EdgeUno 7195 y 273993), de modo que el veredicto los trata como una sola salida. Las rutas se muestran red por red: "Tv&Mas → EdgeUno → Netflix".

## Límites

- Un solo punto de vista: no se ve la ruta de regreso.
- El ASN es el origen del prefijo según Team Cymru, no necesariamente quien opera el router.
- El núcleo MPLS de un proveedor sin propagación de TTL aparece como menos saltos.
- Pérdida en un salto intermedio que no continúa hasta el destino suele ser limitación de ICMP del router.

## Retención

Líneas base: 3 días; las que registraron cambio de ruta y las manuales, 90 días; las de incidente, mientras exista el incidente (366 días). Si el trazador deja de enviar señal aparece **Trazas detenidas** en la barra de estado.

## Trazas incoherentes y concurrencia

Una traza en la que la misma IP aparece en saltos no contiguos se descarta con `trace_inconsistent`: son respuestas atribuidas a otra sonda (o un bucle de enrutamiento) y compararla generaría cambios de ruta falsos. Varios mtr simultáneos en el mismo contenedor se atribuyen respuestas entre sí, con ICMP y con TCP: con 8 a la vez se mezclaba el 20 % de las trazas, y con 4 también ocurrió. Por eso `ODIN_TRACE_CONCURRENCY` vale 1: cada traza dura unos 7,5 s y una sola cola cubre unas 110 comprobaciones en cada ciclo de 15 minutos; por encima, las trazas de base atrasadas se descartan y se reemplazan por las siguientes. Subir la concurrencia vuelve a mezclar respuestas. El tracer y el sondeador corren con `init: true`: mtr deja procesos hijos que, sin un init que los recoja, se acumulan como zombis hasta agotar el límite de procesos del contenedor.

## Trazas en los casos

Un caso de latencia incluye como evidencia la traza tomada al abrirse y al recuperarse cada uno de sus incidentes, y cada cambio de ruta de sus destinos en el período, con el camino antes y después leído red por red (por ejemplo, "antes Tv&Mas → EdgeUno → Riot · ahora Tv&Mas → Cogent → Riot"). Un caso de BGP o de interfaz incluye, como impacto, los cambios de ruta de destinos cuyo camino pasa por ese equipo (requiere el inventario). El analista recibe estas líneas como cualquier otra evidencia. Las trazas con respuestas mezcladas no se incluyen.
