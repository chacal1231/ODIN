# Catálogo HYPERSCALERS

El JSON del catálogo se conserva completo en `probe_catalogs`, con versión y SHA-256. Sus 105 destinos se materializan en `probe_targets`: AWS 16, GCP 30, Azure 26, Oracle 22 e IBM 11. Se conservan proveedor, región, ciudad, país, alcance y ASN de referencia. El ASN no se utiliza como ASN observado ni como restricción de acceso.

La migración `0006_transport_probes` añade los protocolos y el catálogo sin reiniciar las series existentes. El original está en `backend/odin/probes/data/hyperscalers-v1.json`.

## Importación y activación

El servicio `catalogs` de `compose.yaml` se ejecuta una vez en cada arranque (`import_catalog --startup`) y carga y activa `hyperscalers-v2.json`: 12 destinos con las regiones a las que llega el tráfico del ISP. Son Oracle Bogotá (línea base nacional), AWS us-east-1/México/São Paulo, Azure East US/South Central/Brazil South, GCP us-east1/São Paulo, búsqueda de Google y DNS 8.8.8.8/8.8.4.4. Las demás regiones y las IPv6 (el servidor no tiene ruta IPv6) quedan en `hyperscalers-v1.json` como referencia, sin cargarse. Para hacerlo a mano:

```bash
# Guarda el catálogo y comprueba cada endpoint; mantiene los destinos desactivados.
docker compose exec -T probe python -m odin.probes.import_catalog
# Comprueba y activa los destinos válidos; reintenta los pendientes.
docker compose exec -T probe python -m odin.probes.import_catalog --activate
```

La importación es idempotente por destino. Conserva ajustes, destinos archivados y destinos que el operador haya pausado después de su activación. Un documento distinto con la misma versión, o con una versión menor, se rechaza: no sobrescribe silenciosamente el catálogo existente. Una versión mayor reemplaza el documento, mantiene con su historial los destinos que siguen, y archiva los que salen, salvo los que un operador haya activado: esos siguen midiendo hasta que se archiven a mano. Un fallo deja el destino registrado, desactivado y con fecha/motivo de validación visible. Reintentar la importación permite completar las altas pendientes. Las pruebas manuales se consultan en Últimas ejecuciones y no activan destinos ni generan incidentes.

## Mediciones

- ICMP: cuatro destinos DNS de Google, dos IPv4 y dos IPv6. Diez paquetes cada 30 segundos; pérdida y RTT por IP.
- TCP: 91 destinos. Conexión al puerto 443; mide duración de conexión y porcentaje de intentos fallidos.
- HTTPS: diez destinos. Conexión, certificado y nombre TLS verificados, petición HEAD y recepción de cabeceras. Registra tiempos TCP/TLS/total y código HTTP; no sigue redirecciones ni descarga el cuerpo.

TCP/HTTPS usan inicialmente un intento cada 60 segundos, timeout de dos segundos por IP y ventana de cinco minutos. El panel distingue fallos de intentos de pérdida ICMP. La latencia de HTTPS incluye conexión, TLS y cabeceras; no es RTT de red. Los umbrales de latencia quedan sin activar hasta definir valores por región.

Los códigos HTTPS inicialmente aceptados son 200, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404 y 405: comprueban respuesta del endpoint incluso cuando requiere autenticación o rechaza HEAD. Son editables en Destinos. Aceptar un 403 o una redirección no demuestra funcionamiento del servicio. TCP no comprueba TLS ni una transacción de aplicación. Ningún endpoint representa disponibilidad de toda una región.

Los umbrales iniciales son 5 % advertencia y 20 % crítico, con dos evaluaciones consecutivas; tres rondas sin respuesta abren condición crítica. Con solo cinco intentos por ventana, un fallo equivale a 20 %: los porcentajes tienen resolución gruesa. Recuperación por debajo del 2 % durante tres evaluaciones. Cobertura mínima del 80 %. La falta de ruta local, errores de ejecución o DNS son ausencia de datos, nunca pérdida atribuida al proveedor.

## DNS y capacidad

Los destinos del catálogo siguen DNS público automáticamente; cada ronda resuelve hasta 16 IP de la familia elegida. Se rechazan direcciones no públicas y se conecta a la IP numérica validada conservando Host/SNI. Cada IP mantiene su propia serie. La rotación puede iniciar otra ventana de calentamiento. Los PE conservan su política de IP autorizada.

El resolvedor utiliza un proceso aislado ligero, con límite de cinco segundos. Las comprobaciones TCP/HTTPS tienen además un plazo máximo de proceso; una ronda que exceda el presupuesto no se ejecuta. Valores por defecto: 32 trabajadores, máximo 384 IP activas, presupuesto del 70 % de trabajadores y 8 trazas simultáneas. Las IP conocidas cuentan individualmente para el máximo; en el presupuesto, una comprobación ICMP cuesta lo mismo con una o varias IP, porque un solo fping las mide en paralelo, mientras que TCP/HTTPS las recorre una a una. El contenedor dispone de 1,5 GiB, dos CPU y 256 procesos; solo añade NET_RAW para ICMP, sin red del host ni acceso a Docker.

El JSON no selecciona necesariamente una ruta regional del tráfico de clientes: DNS anycast, portales globales y endpoints de control deben interpretarse según su alcance. Las observaciones salen desde el servidor ODIN.

