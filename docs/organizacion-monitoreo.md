# Organización del monitoreo

## Navegación

El menú **Monitoreo** despliega **Red propia** y **Servicios externos**. Al entrar desde el mapa o el resumen, el grupo se abre automáticamente y señala la vista activa.

- **Red propia:** resumen por POP, equipos/destinos y enlaces físicos. Cada POP muestra cuántos equipos del inventario tienen un sondeo asociado. La vista de enlaces conserva puertos y VLAN; distingue comprobaciones directas del enlace de las de sus extremos. No infiere salud del enlace porque sus equipos respondan.
- **Servicios externos:** categorías, proveedores y regiones. Los destinos se despliegan dentro de cada proveedor; búsqueda por nombre, endpoint, proveedor, ciudad/región y filtro de novedades. Las categorías compartidas no duplican comprobaciones.
- **Incidentes:** agrupación operativa por POP para red propia y por destino para servicios externos, manteniendo las señales, reconocimientos y cronologías originales. Una agrupación no confirma una causa común; un incidente abierto no desaparece al archivar su comprobación.
- **Inicio:** atención del NOC ordenada por severidad y cantidad de señales, POPs, servicios externos afectados, salud del sondeador, pendientes/sin datos/pausas y cambios recientes de incidentes. Las fuentes conservan su panel y se actualizan cada 15 segundos.

Desde el mapa se abre el monitoreo del POP seleccionado; al pulsar un enlace se abre su ficha en la vista de red. Las asociaciones a equipo o enlace se editan en **Organizar**, usando selectores con búsqueda integrada. Un equipo que cambia de POP o se concilia se sigue a través del inventario.

## Destino y comprobaciones

`monitoring_destinations` representa el destino lógico: nombre, ámbito, asociación opcional a equipo o enlace y versión. `probe_targets` conserva las comprobaciones existentes y añade `destination_id` y `essential`. El contrato `/targets` sigue disponible para clientes anteriores.

Un destino admite varias comprobaciones ICMP, TCP y HTTPS. Cada una tiene dirección/familia, configuración, perfil, activación, historial e incidentes independientes. **Añadir comprobación** conserva el destino lógico y crea una comprobación desactivada; el operador decide cuándo activarla. No se generan comprobaciones adicionales automáticamente.

Las comprobaciones esenciales determinan el estado agregado. Las opcionales conservan métricas e incidentes y muestran novedades, pero no modifican por sí solas el estado esencial. Sin esenciales, el destino queda sin comprobaciones esenciales. Pausado, mantenimiento, calentamiento y falta de datos son estados distintos de normal. Fallos en parte del grupo producen afectación parcial. El resumen de un proveedor describe los endpoints observados, no toda su infraestructura.

La medición sale del servidor ODIN. ICMP a una dirección de gestión no mide por sí solo la experiencia del tráfico de clientes. El perfil DNS público comprueba conectividad ICMP; las transacciones DNS, métricas de interfaces, eventos de Zabbix/LibreNMS, logs, BMP y correlación causal requieren sus integraciones posteriores. Los cambios recientes de esta entrega son del monitoreo y sus incidentes, no anuncios de rutas.

## Perfiles

Se conservan Estándar y Servicios TCP / HTTPS. Se añaden plantillas reutilizables:

1. PE / infraestructura propia.
2. Enlaces y tránsito.
3. DNS público · conectividad ICMP.
4. Servicios regionales.
5. Servicios globales.
6. Referencia internacional.
7. Juegos · tiempo real.

Las tres primeras y Juegos mantienen la base ICMP de diez paquetes cada 30 segundos; Juegos abre advertencia con 2 % de pérdida y crítico con 10 %. Las demás usan un intento cada 60 segundos y timeout de dos segundos, compatibles con TCP/HTTPS. No se asignan automáticamente ni se inventan umbrales de latencia por región.

El jitter ICMP es la media de la diferencia entre respuestas consecutivas de una misma ronda; un paquete perdido corta el par y nunca se compara entre rondas. Tiene umbrales opcionales de advertencia y crítico (requiere al menos dos paquetes por ronda) y no se evalúa en TCP/HTTPS. Se fija por destino tras observar su línea base. Cambiar una plantilla crea una versión, con aplicación explícita a sus comprobaciones; los ajustes individuales se conservan.

## Migración y permisos

`0007_monitoring_destinations` crea un destino por comprobación existente y rellena su relación sin modificar ID, época, protocolo, configuración o habilitación. Asocia equipos propios únicamente cuando existe una coincidencia única de nombre; otras asociaciones se revisan manualmente. No agrupa automáticamente por IP, porque compartir dirección no demuestra identidad.

Renombrar, cambiar ámbito/asociación o marcar una comprobación como esencial no reinicia sus series. Los cambios de medición mantienen las reglas previas de épocas y resultados obsoletos. No se permite mover una comprobación con historial a otro destino. Solo administradores modifican organización/comprobaciones; operadores consultan y mantienen sus permisos existentes sobre incidentes.

Respaldar PostgreSQL antes del despliegue. Verificar migración, IDs/épocas/configuración antes/después, salud del sondeador, comprobaciones vigentes y acceso autenticado. El rollback de la migración elimina la agrupación; no borra las mediciones originales, pero pierde las asociaciones organizativas nuevas.


## Catálogos y juegos

En cada arranque, el servicio `catalogs` ejecuta `import_catalog --startup`: carga y activa, en este orden de prioridad, juegos, DNS, mensajería, social, streaming, banca/pagos, gobierno, VoIP, SaaS, search/AI y la versión resumida de hyperscalers (12 destinos). Es idempotente: conserva destinos validados, pausados o archivados y reintenta los que fallaron. Un catálogo que no se puede guardar se reporta sin detener los demás. Si la capacidad del sondeador se agota, los destinos restantes quedan sin activar con el error `probe_capacity`.

`python -m odin.probes.import_catalog <archivo>` carga un catálogo revisado de cualquier categoría (salvo Red propia), crea los destinos desactivados y los valida; `--activate` activa los que pasan. Sin argumento carga `hyperscalers-v2.json`; `data/juegos-v1.json` contiene los servidores de juego observados en flujos (Akvorado) que responden a ICMP, Supercell por TCP 9339 y los routers de borde de Riot y Blizzard, cuyos servidores no responden. Un borde mide la entrada a la red del proveedor, no el servidor, y usa el perfil Estándar porque su plano de control limita ICMP. Las IP de servidores de juego rotan: revisar el catálogo con los flujos cada mes.

Cada destino de catálogo guarda su ASN de referencia. Una traza que no llega al destino pero cuyo último salto con respuesta está dentro de ese ASN se marca como "Llegó a la red del destino" y se compara para detectar cambios de ruta, también durante un incidente con el destino caído.

## Servicios de usuarios

Los catálogos `dns`, `mensajeria`, `social`, `streaming`, `banca`, `gobierno`, `voip`, `saas` y `search-ai` (35 destinos) miden los servicios más usados por los clientes. Los DNS públicos (Cloudflare 1.1.1.1/1.0.0.1, Quad9) se miden por ICMP; WhatsApp chat (`g.whatsapp.net`), por TCP 443; el resto, por HTTPS a su sitio. Cada endpoint se verificó desde el sondeador antes de incluirlo, y cada proveedor guarda el ASN observado en ese momento como referencia para las trazas. OpenDNS quedó fuera: responde a 129 ms y pierde 5 % de forma sostenida, lo que daría alertas permanentes.

BBVA, Banco de Bogotá y ChatGPT responden 403 a peticiones automáticas. Se acepta como respuesta del endpoint, pero no demuestra que el servicio funcione para el usuario. Las CDN entregan un nodo según el resolvedor del servidor ODIN; para medir el mismo nodo que reciben los clientes, el servidor debe resolver con los DNS recursivos del ISP.

## Umbrales adaptativos

Cada comprobación aprende su comportamiento normal de su propio historial ([baseline.py](../backend/odin/probes/baseline.py)). El worker ejecuta una pasada cada hora: recalcula la línea base una vez al día con todo el historial disponible (hasta 7 días de rondas crudas), aplica los pasos automáticos y amortigua las oscilaciones.

- **Línea base:** se calcula el p95 de cada ventana de 5 minutos de todas las IP de la comprobación con su protocolo actual, sin incluir las ventanas de incidentes ni de mantenimiento. La base es la mediana; la dispersión, la desviación absoluta mediana (MAD), de modo que los picos aislados no la arrastran. También registra el jitter base, la pérdida y el p95 de la hora pico (19–23 h) frente a la madrugada (3–6 h), según `ODIN_TIMEZONE` (por defecto America/Bogota).
- **Propuesta:** advertencia = el mayor entre base × 1,5, base + 20 ms y base + 5 MAD; crítico = el mayor entre base × 2,5, base + 50 ms y base + 10 MAD. En HTTPS: × 2 / + 50 ms y × 4 / + 150 ms, porque su tiempo incluye TLS y la respuesta. Jitter (ICMP con al menos 2 paquetes): base × 3 o + 5 ms, y × 6 o + 15 ms.
- **Frenos:** cada umbral se mueve como máximo un 25 % respecto del aplicado, para que una degradación lenta siga alertando en vez de volverse normal. Juegos tiene un techo de 150 ms para el crítico; si la base ya lo supera se marca y no se recorta. La pérdida nunca se adapta: una base con pérdida se marca para revisión.
- **Estados:** sin datos suficientes (menos de 24 h), provisional (menos de 7 días o cobertura inferior al 80 %) y lista.
- **Modos por comprobación:** *Manual* usa los umbrales del perfil. *Propuesto* (el modo por defecto) calcula y muestra la propuesta, que un administrador aprueba desde el detalle o con "Aprobar todas las propuestas". *Automático* aplica la propuesta lista como máximo una vez por semana.
- **Aplicación:** el detector superpone los umbrales aplicados a la configuración de la serie al evaluar. Aplicarlos no cambia la versión ni la época, no reinicia series ni vuelve a calentar. Los incidentes guardan los umbrales con que se abrieron.
- **Oscilaciones:** con 4 o más incidentes de menos de 15 minutos cerrados en 24 h, la persistencia sube 2 evaluaciones (máximo 10) durante un día.

"Recalcular líneas base" fuerza una pasada con el historial disponible en ese momento. Cada cálculo y cada aplicación quedan registrados (`probe_baselines` y auditoría); los cálculos no aplicados se conservan un año.
