# Organización del monitoreo

ODIN mide por su cuenta, desde el servidor, la red propia y los servicios externos: ICMP con fping, y TCP o HTTPS con conexiones reales. Este documento explica cómo se organizan los destinos, cómo se evalúan y cómo se ajustan sus umbrales. Los servicios externos que vienen listos están en [Catálogos](catalogos.md); cómo se convierten las mediciones en casos, en [Casos y veredictos](casos-y-veredictos.md).

## Navegación

- **Red propia**: resumen por POP, equipos y destinos, y enlaces físicos. Cada POP muestra cuántos equipos del inventario tienen un sondeo asociado. La vista de enlaces conserva puertos y VLAN, y distingue las comprobaciones directas del enlace de las de sus extremos. No infiere la salud de un enlace porque sus equipos respondan.
- **Servicios externos**: categorías, proveedores y regiones, con búsqueda por nombre, endpoint, proveedor y región, y filtro de novedades.
- **Incidentes**: agrupados por POP en la red propia y por destino en servicios externos. Una agrupación no confirma una causa común. Un incidente abierto no desaparece aunque se archive su comprobación.
- **Inicio → Panorama**: atención del NOC por severidad, POPs, servicios externos afectados (con el veredicto), salud del sondeador y cambios recientes. Se actualiza cada 15 segundos.

Desde el mapa se abre el monitoreo del POP. Las asociaciones a un equipo o enlace se editan en **Organizar**.

## Destinos y comprobaciones

Un **destino** es lo que se vigila: un equipo, un enlace o un servicio. Puede tener varias **comprobaciones** (ICMP, TCP o HTTPS), cada una con su dirección, su perfil, su historial y sus incidentes. **Añadir comprobación** crea una nueva desactivada; el operador decide cuándo activarla.

- **Esenciales y opcionales.** Las comprobaciones esenciales definen el estado del destino. Las opcionales guardan métricas e incidentes, pero no cambian ese estado por sí solas. Si falla solo una parte de las esenciales, el destino queda con **afectación parcial**.
- **Estados distintos de "normal":** pausado, mantenimiento, calentamiento (los primeros minutos de una serie nueva) y sin datos.
- **Direcciones.** Una comprobación por IP usa direcciones autorizadas. Una por nombre DNS público resuelve en cada ronda hasta 16 IP públicas y mantiene una serie por IP. Las direcciones privadas, de loopback o de enlace local se rechazan.
- **Mantenimientos.** Se programan por destino. Durante un mantenimiento no se abren incidentes y sus mediciones no entran en las líneas base.
- **Límites.** Hasta 200 destinos registrados, sujetos a la capacidad del sondeador ([Configuración](configuracion.md)).

ICMP a una IP de gestión mide la conectividad hasta ese equipo, no la experiencia de los clientes.

## Detección

En cada ronda se evalúa una **ventana de 5 minutos**:

| Condición | Por defecto |
|---|---|
| Pérdida (ICMP) o intentos fallidos (TCP/HTTPS) | Advertencia 5 %, crítico 20 %, recuperación por debajo de 2 % |
| Latencia p95 | Sin umbral fijo: se ajusta por destino (umbrales adaptativos) |
| Jitter (ICMP) | Sin umbral fijo: se ajusta por destino |
| Sin respuesta | Crítico tras 3 rondas sin respuesta |

- Hacen falta **2 evaluaciones** seguidas para abrir o escalar un incidente y **3** para recuperarlo. También hace falta un **80 % de cobertura** de la ventana.
- La falta de datos nunca cuenta como pérdida.
- Al abrirse y al recuperarse cada incidente, el tracer toma una traza ([Trazas de ruta](trazas-de-ruta.md)).

El **jitter** es la media de la diferencia entre respuestas consecutivas dentro de una misma ronda. Un paquete perdido corta el par, y nunca se comparan rondas distintas. Requiere al menos 2 paquetes por ronda y no se evalúa en TCP/HTTPS.

## Perfiles

Un perfil es un conjunto de parámetros con versiones. Cambiar un perfil crea una versión nueva, que se aplica a sus comprobaciones de forma explícita, y los ajustes individuales se conservan. Plantillas incluidas:

| Perfil | Medición | Uso |
|---|---|---|
| Estándar | ICMP, 10 paquetes cada 30 s | General |
| PE / infraestructura propia | Igual que Estándar | Gestión de los equipos propios |
| Enlaces y tránsito | Igual que Estándar | Otro extremo de los enlaces con proveedores |
| DNS público · conectividad ICMP | Igual que Estándar | Resolvedores públicos |
| Juegos · tiempo real | ICMP, 10 paquetes cada 30 s; pérdida 2 % / 10 % | Servidores de juego |
| Servicios TCP / HTTPS | 1 intento cada 60 s, timeout de 2 s | Sitios y APIs |
| Servicios regionales / globales / Referencia internacional | Igual que TCP / HTTPS | Servicios externos según su alcance |

Cambiar un parámetro de **medición** (dirección, protocolo, paquetes, intervalo) reinicia la serie de la comprobación. Renombrarla, cambiar su asociación o marcarla como esencial, no.

## Umbrales adaptativos

Cada comprobación aprende su comportamiento normal de su propio historial ([baseline.py](../backend/odin/probes/baseline.py)). El worker hace una pasada cada hora: recalcula la línea base una vez al día, con hasta 7 días de mediciones, aplica los pasos automáticos y amortigua las oscilaciones.

**Línea base**
- Es el p95 de cada ventana de 5 minutos, de todas las IP de la comprobación con su protocolo actual, sin las ventanas de incidentes ni de mantenimiento.
- La base es la mediana de esos p95. La dispersión es su desviación absoluta mediana (MAD), así que los picos aislados no la arrastran.
- También registra el jitter base, la pérdida y el p95 de la hora pico (19–23 h) frente a la madrugada (3–6 h), según `ODIN_TIMEZONE`. Si la diferencia entre pico y madrugada crece semana a semana, suele indicar congestión.

**Propuesta**

| | Advertencia | Crítico |
|---|---|---|
| ICMP y TCP | el mayor entre base × 1,5, base + 20 ms y base + 5 MAD | el mayor entre base × 2,5, base + 50 ms y base + 10 MAD |
| HTTPS (incluye TLS y la respuesta) | base × 2 o + 50 ms | base × 4 o + 150 ms |
| Jitter (ICMP) | base × 3 o + 5 ms | base × 6 o + 15 ms |

Por ejemplo, un relay de juegos con base de 38 ms queda en 58 / 95 ms, y un destino local de 1,5 ms, en 21,5 / 51,5 ms.

**Frenos**
- Cada umbral se mueve como máximo un **25 %** respecto del aplicado. Así una degradación lenta sigue alertando en vez de volverse normal.
- Los juegos tienen un **techo** de 150 ms para el crítico. Si la base ya lo supera, se marca y no se recorta.
- **La pérdida nunca se adapta.** Un camino sano no pierde paquetes: una base con pérdida se marca para revisión.

**Estados de la propuesta:** *sin datos suficientes* (menos de 24 horas), *provisional* (menos de 7 días o cobertura menor al 80 %) y *lista*.

**Modos por comprobación**
- **Manual:** usa los umbrales del perfil.
- **Propuesto** (por defecto): ODIN calcula y muestra la propuesta; un administrador la aprueba desde el detalle del destino, o todas juntas con **Aprobar todas las propuestas**.
- **Automático:** aplica la propuesta lista como máximo una vez por semana.

**Aplicación:** el detector superpone los umbrales aplicados a la configuración de la serie al evaluar. Aplicarlos no cambia la versión ni reinicia la serie. Los incidentes guardan los umbrales con los que se abrieron.

**Oscilaciones:** con 4 o más incidentes de menos de 15 minutos cerrados en 24 horas, la persistencia sube 2 evaluaciones (máximo 10) durante un día.

**Recalcular líneas base** fuerza una pasada con el historial disponible. Cada cálculo y cada aplicación quedan registrados.

## Retención

| Dato | Se conserva |
|---|---|
| Rondas crudas (cada muestra) | 7 días |
| Resúmenes de 5 minutos | 90 días |
| Resúmenes por hora | 1 año |
| Incidentes cerrados | 1 año |
| Líneas base no aplicadas | 1 año |

## Permisos

Los administradores modifican destinos, comprobaciones, perfiles, organización, mantenimientos y umbrales, y lanzan pruebas manuales. Los operadores consultan, reconocen incidentes, agregan observaciones y piden trazas manuales (hasta 10 cada 5 minutos).

## Notas de actualización

La migración `0007_monitoring_destinations` creó un destino por cada comprobación existente sin modificar sus ID, épocas ni configuración. Asoció equipos propios solo cuando había una única coincidencia de nombre. No agrupa por IP, porque compartir una dirección no demuestra identidad.
