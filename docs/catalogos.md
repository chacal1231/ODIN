# Catálogos de servicios externos

Un catálogo es una lista revisada de destinos externos de una categoría: juegos, DNS, redes sociales, streaming, etc. Cada destino trae su endpoint, su protocolo y el ASN de su red. ODIN los carga en cada arranque, los valida desde el sondeador y los activa. Con ellos responde, para cada servicio que usan los clientes, si el problema es nuestro, de una salida o del servicio ([Casos y veredictos](casos-y-veredictos.md)).

## Catálogos incluidos

Se cargan y activan en este orden de prioridad. Si el sondeador se queda sin capacidad, lo último es lo que queda sin activar.

| # | Archivo | Destinos | Qué mide |
|---|---|---|---|
| 1 | `juegos-v1.json` | 21 | Servidores y relays de juego: Valve (Lima), Free Fire, Tencent (PUBG/COD Mobile), Multiplay, EA, PSN, Blizzard, Fortnite (NA-East y Brasil); Supercell por TCP 9339; routers de borde de Riot y Blizzard |
| 2 | `dns-v1.json` | 3 | DNS públicos por ICMP: Cloudflare 1.1.1.1 y 1.0.0.1, Quad9 |
| 3 | `mensajeria-v1.json` | 2 | WhatsApp web (HTTPS) y chat (`g.whatsapp.net`, TCP 443) |
| 4 | `social-v1.json` | 3 | Facebook, Instagram, TikTok |
| 5 | `streaming-v1.json` | 8 | YouTube, Netflix y fast.com, Disney+, Prime Video, HBO Max, Twitch, Spotify |
| 6 | `banca-v1.json` | 7 | PSE, Bancolombia, Nequi, Davivienda, Daviplata, BBVA, Banco de Bogotá |
| 7 | `gobierno-v1.json` | 3 | DIAN, MUISCA, GOV.CO |
| 8 | `voip-v1.json` | 4 | Teams, Zoom, Google Meet, Discord |
| 9 | `saas-v1.json` | 2 | Outlook 365, Gmail |
| 10 | `search-ai-v1.json` | 3 | ChatGPT, Gemini, Claude |
| 11 | `hyperscalers-v2.json` | 12 | Oracle Bogotá (línea base nacional), AWS us-east-1, México y São Paulo; Azure East US, South Central y Brazil South; GCP us-east1 y São Paulo; búsqueda de Google; DNS 8.8.8.8 y 8.8.4.4 |

En total son 68 destinos, que consumen unos 12 de los 22,4 de capacidad del sondeador ([Configuración](configuracion.md)).

`hyperscalers-v1.json` (105 destinos: todas las regiones de AWS, GCP, Azure, Oracle e IBM, más IPv6) queda como referencia y no se carga.

Los catálogos de banca y gobierno son de Colombia, y el de juegos se armó con los flujos de un ISP colombiano. **Cada ISP debería revisarlos con su propia red** (ver abajo).

## Qué se mide

| Protocolo | Medición | Perfil |
|---|---|---|
| ICMP | 10 paquetes cada 30 s; pérdida, latencia p95 y jitter por IP | Estándar; los juegos usan *Juegos · tiempo real* (advertencia con 2 % de pérdida, crítico con 10 %) |
| TCP | Conexión al puerto: duración e intentos fallidos | Servicios TCP / HTTPS: un intento cada 60 s, timeout de 2 s |
| HTTPS | Conexión, certificado y nombre TLS verificados, petición HEAD y cabeceras; tiempos TCP/TLS/total y código HTTP | Igual que TCP |

- La latencia HTTPS incluye la conexión, TLS y la respuesta: no es el RTT de red.
- Los códigos aceptados por defecto (200, 204, 3xx, 400, 401, 403, 404, 405) prueban que el **endpoint responde**, no que el servicio funcione. BBVA, Banco de Bogotá y ChatGPT responden 403 a peticiones automáticas.
- Un **router de borde** (`scope: border`, Riot y Blizzard) no es el servidor: responde ICMP desde su plano de control y puede limitarlo. Se usa cuando los servidores de juego no responden, para seguir al menos la entrada a la red del proveedor. Usa el perfil Estándar.
- La falta de ruta local, los errores de ejecución o de DNS se registran como **ausencia de datos**, nunca como pérdida atribuida al proveedor.
- Los destinos por nombre resuelven en cada ronda hasta 16 IP públicas, con el DNS del servidor. **Usar los recursivos del ISP**: con un DNS público, una CDN puede entregar otro nodo que el de los clientes.

Los umbrales de latencia y jitter no vienen fijados: los propone cada destino a partir de su propio historial ([umbrales adaptativos](organizacion-monitoreo.md#umbrales-adaptativos)).

## Cómo se cargan

El servicio `catalogs` corre una vez en cada arranque (`python -m odin.probes.import_catalog --startup`):

1. Guarda cada catálogo en `probe_catalogs` (documento completo, versión y SHA-256).
2. Crea los destinos que faltan, con categoría, servicio, proveedor y ASN de referencia.
3. Valida cada destino desde la red del sondeador y activa los que responden.

La carga es idempotente:

- **Conserva** los ajustes, los destinos archivados y los que un operador pausó.
- **Reintenta** los que fallaron.
- Un catálogo que no se puede guardar se reporta sin detener los demás.

El resultado de cada arranque se ve con `docker compose logs catalogs`.

Para cargar un catálogo a mano:

```bash
docker compose exec -T probe python -m odin.probes.import_catalog odin/probes/data/mi-catalogo-v1.json             # valida, no activa
docker compose exec -T probe python -m odin.probes.import_catalog odin/probes/data/mi-catalogo-v1.json --activate  # valida y activa
```

### Versiones

- Un archivo distinto **con la misma versión**, o con una versión menor, se rechaza: no sobrescribe en silencio lo guardado.
- Una **versión mayor** reemplaza el documento:
  - mantiene, con su historial, los destinos que siguen;
  - **archiva** los que salen, salvo los que un operador haya activado, que siguen midiendo hasta que se archiven a mano.

Así se pasó de `hyperscalers-v1` (105 destinos) a `hyperscalers-v2` (12).

## Formato

```json
{
  "category": "JUEGOS",
  "version": 1,
  "providers": [
    {
      "provider": "VALVE",
      "asn": 32590,
      "service": "Steam/Valve",
      "targets": [
        {"name": "VALVE-SDR-LIM-1", "target": "155.133.244.51", "protocol": "icmp", "city": "Lima", "country": "PE", "scope": "regional"}
      ]
    }
  ]
}
```

| Campo | Valores |
|---|---|
| `category` | Nombre de una categoría de ODIN: JUEGOS, DNS, MENSAJERÍA, SOCIAL, STREAMING, CDN, BANCA/PAGOS, GOBIERNO, VOIP/RTC, SAAS, SEARCH/AI, HYPERSCALERS, ECOMMERCE, SPEEDTEST, INTERNACIONAL (no RED PROPIA) |
| `provider` | Identificador único dentro del catálogo |
| `asn` | ASN de la red del proveedor: la referencia para el veredicto y para comparar trazas que no llegan al destino |
| `service` | Nombre que ven el NOC y los casos; obligatorio salvo en HYPERSCALERS |
| `name` | Nombre único del destino en toda la instalación |
| `target` | IP o nombre DNS (sin URL ni puerto) |
| `protocol` | `icmp`, `icmp6`, `tcp`, `https` (`tcp` y `https` exigen `port`) |
| `scope` | `regional`, `anycast`, `edge`, `api`, `border` (router de borde, no el servidor) |
| `asn` del destino | Opcional, cuando ese endpoint vive en otra red que la del proveedor (por ejemplo, fast.com en Akamai) |

## Adaptar los catálogos a tu red

Lo que importa es medir **lo que tus clientes usan** y **desde donde lo usan**. El método con el que se armaron los catálogos incluidos:

1. **Ver qué consume la red.** En los flujos (Akvorado, NetFlow, sFlow), buscar los sistemas autónomos con más tráfico y, para juegos, las IP de servidores con tráfico UDP (Riot AS6507, Valve AS32590, Roblox AS22697, Blizzard AS57976, Garena AS58521…). Elegir 2 o 3 IP por servicio, con las de más volumen.
2. **Verificar desde el servidor ODIN** cada candidato: si responde a ICMP (`ping`), su ASN real (`dig TXT <ip-invertida>.origin.asn.cymru.com`), si acepta TCP en su puerto, y hasta dónde llega la traza (`mtr`). Lo que no responde a nada no sirve; si la traza entra a la red del proveedor, su **router de borde** sí sirve.
3. **Descartar lo ruidoso.** Un destino que pierde paquetes de forma sostenida (OpenDNS perdía 5 % a 129 ms) daría alertas permanentes.
4. **Escribir el JSON** en `backend/odin/probes/data/`, con el ASN observado de cada proveedor.
5. **Agregarlo a la lista de arranque** (`STARTUP` en `odin/probes/import_catalog.py`), en el orden de prioridad que corresponda.
6. Para cambiar un catálogo ya cargado, **subir su versión**.

Las IP de servidores de juego rotan: conviene revisar ese catálogo con los flujos **cada mes**.
