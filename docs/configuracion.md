# Configuración

La configuración vive en tres lugares:

- **`.env`**: variables de entorno `ODIN_*`. Lo crea y completa `scripts/setup-local.sh`, que nunca regenera los secretos y conserva las demás líneas.
- **`.secrets/master.key`**: la clave maestra.
- **La interfaz**: fuentes, organización del monitoreo, guías, turnos y WhatsApp, guardados cifrados o en la base.

## Variables de entorno

| Variable | Por defecto | Para qué |
|---|---|---|
| `ODIN_DATABASE_URL` | — (obligatoria) | `postgresql+psycopg://…`; la genera el script de instalación. |
| `ODIN_MASTER_KEY_FILE` | — (obligatoria) | Ruta de la clave maestra dentro de los contenedores (`/run/secrets/master.key`). |
| `ODIN_ORIGIN` | `http://localhost:8080` | Origen exacto con el que se entra a ODIN, sin barra final. Las mutaciones se rechazan si no coincide. Los Compose WAN y público lo fijan solos. |
| `ODIN_SECURE_COOKIES` | `true` | `false` solo se admite con un origen `http://localhost` o `http://127.0.0.1`. |
| `ODIN_ISP_NAME` | vacío | Nombre del ISP que se muestra junto a ODIN (hasta 60 caracteres). |
| `ODIN_TIMEZONE` | `America/Bogota` | Zona horaria de los turnos, de los mensajes y de la hora pico de los umbrales adaptativos. |
| `ODIN_ASN` | detectado | ASN propio del ISP. Sin definir, ODIN toma el sistema autónomo en el que empiezan la mayoría de las trazas. |
| `ODIN_PROBE_CONCURRENCY` | `32` | Trabajadores del sondeador (1–32). El presupuesto usable es el 70 %. |
| `ODIN_PROBE_MAX_ADDRESSES` | `384` | IP medidas a la vez como máximo (1–512). |
| `ODIN_TRACE_CONCURRENCY` | `1` | Trazas simultáneas. **Dejar en 1**: varios mtr en el mismo contenedor se mezclan las respuestas. |
| `ODIN_WHATSAPP_URL` | `http://whatsapp:3000` | Servicio de WhatsApp dentro de la red de Compose. |
| `ODIN_MAP_TILE_URL` | OpenStreetMap | Teselas del mapa. Si se cambia, también hay que permitir su dominio en `img-src` del `frontend/Caddyfile`. |
| `ODIN_IDLE_SECONDS` | `1800` | Cierre de sesión por inactividad. |
| `ODIN_SESSION_SECONDS` | `43200` | Duración máxima de una sesión. |
| `ODIN_WEB_PORT` | `8080` | Puerto local de la web (solo en `127.0.0.1`). |
| `ODIN_WAN_HOST`, `ODIN_WAN_PORT`, `ODIN_WAN_ALLOW` | — | Publicación por IP pública con `compose.wan.yaml`. |
| `ODIN_DOMAIN` | — | Dominio del panel con `compose.public.yaml`; Caddy obtiene su certificado. |
| `COMPOSE_FILE` | — | La publicación elegida, por ejemplo `compose.yaml:compose.wan.yaml`, para que `docker compose up` la use siempre. La define el script de instalación. |

## DNS del servidor

ODIN resuelve los servicios externos con el resolvedor del servidor. **Debe ser el recursivo del ISP**, el mismo que usan los clientes. Con 8.8.8.8 o 1.1.1.1, las CDN (YouTube, Netflix, TikTok, Akamai) pueden entregar un nodo distinto al de los clientes, y ODIN mediría otro camino. Con systemd-resolved, en `/etc/systemd/resolved.conf`:

```
[Resolve]
DNS=<recursivo-1> <recursivo-2>
```

## Capacidad del sondeador

Cada comprobación activa consume una parte del tiempo de los trabajadores:

| Tipo | Costo por comprobación |
|---|---|
| ICMP, 10 paquetes cada 30 s | 0,27 (igual con 1 o con 16 IP: un fping las mide en paralelo) |
| TCP/HTTPS, 1 intento cada 60 s | (2 s × IP + 3 s) / 60 s; por ejemplo, 0,08 con una IP y 0,58 con 16 |

El presupuesto es `ODIN_PROBE_CONCURRENCY × 0,7` (22,4 por defecto). Si una comprobación nueva lo excede, se rechaza con un aviso y los catálogos dejan sin activar lo que no cabe (`probe_capacity`).

Como referencia, los catálogos de arranque consumen unos 12 en total. Quedan unos 10 para la red propia, es decir, unos 38 equipos con el perfil PE.

El tracer hace una traza de base cada 15 minutos por comprobación activa, de a una por vez (unos 7,5 s cada una). Así cubre unas 110 comprobaciones; por encima, las trazas de base atrasadas se descartan y se reemplazan por las del ciclo siguiente.

## Actualizar

```bash
# Antes: respaldo de la base y de la clave maestra
docker compose exec -T db pg_dump -U odin -Fc odin > odin-$(date +%F).dump
cp .secrets/master.key /lugar/seguro/

git pull
docker compose up -d --build        # aplica las migraciones antes de arrancar los procesos
docker compose logs --tail=50 migrate catalogs
```

Después de actualizar, revisar la franja de salud de la bandeja y `docker compose logs catalogs`, que muestra la validación de cada destino de los catálogos.

## Respaldos y restauración

- **PostgreSQL**: `pg_dump` como arriba. Para restaurar sobre una instalación detenida: `docker compose up -d db` y luego `docker compose exec -T db pg_restore -U odin -d odin --clean < archivo.dump`.
- **Clave maestra**: sin ella no se descifran el MFA ni las credenciales de las fuentes, y cambia el token de WhatsApp. Guardarla aparte de la base.
- **Sesión de WhatsApp**: está en el volumen `whatsapp_data`. Si se pierde, basta con volver a escanear el QR.

## Problemas frecuentes

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| La web no responde desde afuera tras actualizar | Se levantó sin el Compose de publicación | `docker compose -f compose.yaml -f compose.wan.yaml up -d`, o definir `COMPOSE_FILE` en `.env` |
| "Trazas detenidas" y `mtr_unavailable` en los logs del tracer | El contenedor agotó sus procesos | Verificar `init: true` en `tracer` y recrearlo |
| Cambios de ruta que no existen | Trazas simultáneas (`ODIN_TRACE_CONCURRENCY` > 1) | Volver a 1 |
| WhatsApp "no responde" | El servicio `whatsapp` está caído o arrancando | `docker compose logs whatsapp`; la primera vez Chromium tarda en iniciar |
| WhatsApp pide el QR de nuevo | La sesión se cerró en el teléfono o se borró el volumen | Volver a escanear en Comunicación NOC |
| Destinos de catálogo con `probe_capacity` | Se acabó el presupuesto del sondeador | Subir `ODIN_PROBE_CONCURRENCY`, bajar intervalos o archivar destinos |
| Servicios de CDN con latencias altas y raras | El servidor resuelve con DNS públicos | Usar los recursivos del ISP |
| "Sin datos de clientes" en el impacto | Falta SmartOLT o el inventario | Conectarlos en Puesta en marcha |
| Recuperar acceso de un usuario | Contraseña o MFA perdidos | `docker compose exec api python -m odin.cli reset-access usuario` |
