# Arquitectura

ODIN se instala con Docker Compose: un PostgreSQL y varios procesos pequeños, cada uno con una sola responsabilidad. Todos leen y escriben la misma base de datos y se coordinan con una cola de trabajos y bloqueos de PostgreSQL, sin brokers adicionales.

## Servicios

| Servicio | Proceso | Qué hace |
|---|---|---|
| `db` | PostgreSQL 17 | Todo el estado: inventario, eventos, mediciones, trazas, casos, informes y auditoría. |
| `migrate` | `alembic upgrade head` | Aplica las migraciones y termina; los demás arrancan después. |
| `api` | FastAPI (`odin.main`) | API para la interfaz. Evalúa la salud de ODIN en cada consulta del panel. |
| `web` | Caddy | Sirve la interfaz (React) y hace de proxy hacia la API. Con `compose.wan.yaml` o `compose.public.yaml` termina HTTPS. |
| `worker` | `odin.worker` | Cola de trabajos: sincronización de inventario, lectura de eventos (Zabbix, LibreNMS, Graylog), SmartOLT, correlación de casos y umbrales adaptativos. |
| `analyst` | `odin.analyst.worker` | Corre los análisis del LLM (llama.cpp local o DeepSeek), que tardan minutos. |
| `probe` | `odin.probes.runner` | Sondeo ICMP (fping), TCP y HTTPS; detector de incidentes; agregación y retención de mediciones. |
| `tracer` | `odin.probes.trace` | mtr hacia cada comprobación, de a una traza por vez; nombres de las redes atravesadas. |
| `telegram` | `odin.telegram` | Grupo del NOC y de proveedores en Telegram; ciclo de envío de WhatsApp; entregas de turno. |
| `whatsapp` | Node (`whatsapp/`) | whatsapp-web.js con Chromium: vinculación por QR, grupos de la cuenta y envío. Solo accesible dentro de la red de Compose. |
| `catalogs` | `odin.probes.import_catalog --startup` | Corre una vez en cada arranque: carga, valida y activa los catálogos de servicios externos. |

## Flujo de datos

```
LibreNMS ──► inventario (equipos, puertos, descripciones) ──┐
Zabbix / LibreNMS ──► alertas ─────────────────────────────┤
Graylog ──► logs de BGP e interfaces ──────────────────────┼──► correlador (cada minuto) ──► casos
SmartOLT ──► OLTs, PON, uplinks, caídas ───────────────────┤        │ lectura, veredicto, impacto
probe ──► rondas ──► detector ──► incidentes ──────────────┤        ▼
tracer ──► trazas, cambios de ruta, nombres de redes ──────┘   bandeja · analista · Telegram/WhatsApp
```

- El **correlador** convierte cambios de estado en casos con evidencia explicada. Cada minuto les agrega la lectura de acceso, el veredicto de servicios externos y el impacto en clientes ([Casos y veredictos](casos-y-veredictos.md)).
- El **detector** evalúa cada ronda contra la configuración de su serie y los umbrales adaptativos aplicados ([Organización del monitoreo](organizacion-monitoreo.md)).
- El **analista** lee un paquete de evidencia del caso y responde citándola.
- El proceso de **mensajería** publica lo que avisa y los informes del N1, y genera las entregas de turno ([Comunicación del NOC](comunicacion-noc.md)).

## Recursos y endurecimiento

| Servicio | Límites | Endurecimiento |
|---|---|---|
| `probe` | 2 CPU, 1,5 GiB, 256 procesos | Solo `NET_RAW`, raíz de solo lectura, `no-new-privileges`, `init` |
| `tracer` | 1 CPU, 512 MiB, 64 procesos | Igual que `probe` |
| `whatsapp` | 1 GiB, 256 procesos, `/dev/shm` de 256 MiB | Sin capacidades, `no-new-privileges`, usuario sin privilegios |
| `catalogs` | 512 MiB, 64 procesos | Igual que `probe` |

`probe` y `tracer` corren con `init: true`: mtr y los procesos auxiliares dejan hijos que, sin un init que los recoja, se acumularían como zombis hasta agotar el límite de procesos.

## Conexiones salientes

| Destino | Quién | Para qué | Qué sale |
|---|---|---|---|
| LibreNMS, Zabbix, Graylog, llama.cpp | `worker`, `analyst` | Leer las fuentes del ISP | Credenciales de solo lectura; solo a las redes permitidas |
| SmartOLT (`*.smartolt.com`) | `worker` | Leer OLTs y caídas | API key; solo direcciones públicas |
| api.telegram.org | `telegram` | Grupos del NOC y proveedores | Casos, lecturas, impacto, diagnósticos, informes |
| WhatsApp Web | `whatsapp` | Grupo del NOC | Lo mismo que Telegram |
| api.deepseek.com (opcional) | `analyst` | Analista por API | La evidencia del caso |
| Team Cymru (DNS) | `tracer` | ASN de cada salto y nombre de cada red | Números de ASN y direcciones de los saltos |
| Destinos de los catálogos | `probe`, `tracer` | Sondeo y trazas | ICMP, conexiones TCP y peticiones HEAD |
| Servidor de mapas | Navegador | Teselas del mapa (`ODIN_MAP_TILE_URL`) | Coordenadas que se ven |

ODIN no abre puertos hacia adentro, salvo el de la web: Telegram se lee por polling y WhatsApp mantiene su propia sesión saliente.

## Un solo punto de vista

Todas las mediciones y trazas salen del servidor ODIN. Eso define lo que ODIN puede afirmar:

- Ve la ruta de ida, no la de vuelta.
- Mide los servicios con el resolvedor DNS del servidor: hay que usar los recursivos del ISP para que las CDN entreguen el mismo nodo que a los clientes.
- Un veredicto o una lectura es la interpretación de esas mediciones, no una causa confirmada.
