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

Las tres primeras mantienen la base ICMP de diez paquetes cada 30 segundos; las otras usan un intento cada 60 segundos y timeout de dos segundos, compatibles con TCP/HTTPS. No se asignan automáticamente ni se inventan umbrales de latencia por región. Cambiar una plantilla crea una versión, con aplicación explícita a sus comprobaciones; los ajustes individuales se conservan.

## Migración y permisos

`0007_monitoring_destinations` crea un destino por comprobación existente y rellena su relación sin modificar ID, época, protocolo, configuración o habilitación. Asocia equipos propios únicamente cuando existe una coincidencia única de nombre; otras asociaciones se revisan manualmente. No agrupa automáticamente por IP, porque compartir dirección no demuestra identidad.

Renombrar, cambiar ámbito/asociación o marcar una comprobación como esencial no reinicia sus series. Los cambios de medición mantienen las reglas previas de épocas y resultados obsoletos. No se permite mover una comprobación con historial a otro destino. Solo administradores modifican organización/comprobaciones; operadores consultan y mantienen sus permisos existentes sobre incidentes.

Respaldar PostgreSQL antes del despliegue. Verificar migración, IDs/épocas/configuración antes/después, salud del sondeador, comprobaciones vigentes y acceso autenticado. El rollback de la migración elimina la agrupación; no borra las mediciones originales, pero pierde las asociaciones organizativas nuevas.

