# ODIN · Edición ISP

Consola de NOC para ISP, instalada una por operador y pensada para el **nivel 1**: quien vigila la red e informa las caídas. ODIN busca que el N1 responda en menos de un minuto tres preguntas:

1. **¿Es nuestro o es de afuera?** Para servicios externos, un veredicto (problema propio, de una salida a internet o del propio servicio). Para el acceso, la lectura de cada OLT (corte de fibra, falla de energía, uplink caído o la OLT inalcanzable).
2. **¿A quién y a cuántos afecta?** ONUs afectadas por OLT, PON y zona, confirmadas por SmartOLT o estimadas por la topología del inventario.
3. **¿Qué hago ahora?** Una guía por situación: qué verificar, el informe para el grupo del NOC y cuándo escalar a N2.

Integra **LibreNMS** y **Zabbix** (inventario y alertas), **Graylog** (logs de los routers) y **SmartOLT** (OLTs y caídas de PON). Suma sondeos ICMP/TCP/HTTPS y trazas propias hacia la red y hacia los servicios que más usan los clientes. Con eso arma casos correlacionados, que un analista LLM (local o DeepSeek) explica citando la evidencia, y los publica en el grupo del NOC por **Telegram** y **WhatsApp**.

ODIN **solo lee** las fuentes: no configura equipos, no reinicia ONUs y no abre tickets.

## Qué hace

| Área | Qué hace | Documento |
|---|---|---|
| Bandeja | Lo que necesita a una persona, lo que está en curso y lo resuelto en el turno; ordenado por severidad, plazo de informe vencido e impacto | [Guía para N1](docs/guia-n1.md) |
| Casos | Correlación determinista de sesiones BGP, interfaces, latencia y acceso (OLT/PON), con evidencia explicada, veredicto e impacto | [Casos y veredictos](docs/casos-y-veredictos.md) |
| Monitoreo propio | Sondeos hacia equipos, enlaces con proveedores y servicios externos, con umbrales adaptativos por línea base | [Organización del monitoreo](docs/organizacion-monitoreo.md) |
| Catálogos | Juegos, DNS, mensajería, redes sociales, streaming, banca, gobierno, VoIP, SaaS, IA y nubes, cargados al arrancar | [Catálogos](docs/catalogos.md) |
| Trazas | mtr hacia cada destino cada 15 minutos y en cada incidente; detección de cambios de ruta y nombres de redes | [Trazas de ruta](docs/trazas-de-ruta.md) |
| Acceso | OLTs, PON, uplinks y caídas de SmartOLT, en solo lectura | [OLTs con SmartOLT](docs/olts-smartolt.md) |
| Comunicación | Guías para N1, informes al grupo del NOC, plazo para informar, escalamiento a N2 y entrega de turno | [Comunicación del NOC](docs/comunicacion-noc.md) |
| Analista | Explica cada caso citando la evidencia y aprende de las valoraciones del NOC | Más abajo |
| Autovigilancia | ODIN avisa cuando su propia cadena de datos se detiene | [Vigilancia de ODIN](docs/vigilancia-odin.md) |

## Requisitos

- Un servidor Linux con Docker Engine y Docker Compose v2.24 o posterior. En Debian y Ubuntu, el script de instalación los instala si faltan. No hace falta instalar Node ni Python en el host.
- Orientativo para un ISP mediano: 4 vCPU, 8 GB de RAM y 50 GB de disco. El sondeador usa hasta 2 CPU y 1,5 GB; WhatsApp (Chromium), hasta 1 GB.
- Que el servidor llegue a las fuentes del ISP (LibreNMS, Zabbix, Graylog). Si falta alguna, [`stack/`](stack/README.md) instala cada una lista para ODIN. También debe llegar a internet (SmartOLT, Telegram, WhatsApp y, si se elige, DeepSeek) y a los destinos que mide.
- **Resolver con los DNS recursivos del ISP**, no con resolvedores públicos. Así las CDN (YouTube, Netflix, TikTok) le entregan a ODIN el mismo nodo que a los clientes.

Arquitectura, servicios, límites de recursos y conexiones salientes: [Arquitectura](docs/arquitectura.md).

## Instalación

```bash
bash scripts/setup-local.sh
```

El script:
- instala Docker y Compose si faltan;
- genera la clave maestra y la contraseña de PostgreSQL;
- pregunta el nombre del ISP, la zona horaria y cómo se publica el panel, y escribe `.env`;
- si se le pide, construye, levanta ODIN y crea el primer administrador.

Se puede volver a ejecutar para cambiar la publicación: nunca regenera los secretos, ofrece los valores actuales y conserva las demás líneas de `.env`. Sin terminal toma los valores por defecto y no levanta nada. Para hacerlo a mano:

```bash
docker compose up --build -d
docker compose exec api python -m odin.cli create-admin nocadmin
```

La CLI pide una contraseña temporal sin mostrarla. En el primer ingreso se exige una contraseña nueva de al menos 14 caracteres y el registro de MFA en un autenticador TOTP (código QR o clave), y se muestran ocho códigos de recuperación. No existen usuarios ni contraseñas predeterminadas.

El script ofrece tres formas de publicar el panel. Guarda la elegida en `COMPOSE_FILE` dentro de `.env`, así que `docker compose` la usa sin repetir los `-f`:

- **Local**: la web escucha solo en `127.0.0.1:8080` (`ODIN_WEB_PORT`). Desde otra máquina, entrar con un túnel SSH que conserve `localhost` como origen.
- **WAN**: por la IP pública en un puerto propio, con HTTPS de la CA interna de Caddy (`compose.wan.yaml`). Usa `ODIN_WAN_HOST` (IP pública), `ODIN_WAN_PORT` (8090 por defecto) y, opcionalmente, `ODIN_WAN_ALLOW` con las redes autorizadas.
- **Dominio**: dominio propio con los puertos 80 y 443 libres y certificado de Let's Encrypt (`compose.public.yaml`). Usa `ODIN_DOMAIN`.

Docker publica el puerto por encima de `ufw`: si solo se debe entrar desde el NOC, restringirlo con `ODIN_WAN_ALLOW`. No publicar el Compose de desarrollo tal cual ni desactivar `Secure` para un dominio público.

Todas las variables de entorno, la capacidad del sondeador, las actualizaciones y los respaldos están en [Configuración](docs/configuracion.md).

## Primeros pasos

1. **Configuración → Puesta en marcha**: conectar LibreNMS, revisar los proveedores detectados en las descripciones de los puertos, sumar Zabbix, Graylog y SmartOLT, y crear los destinos sugeridos ([Puesta en marcha](docs/puesta-en-marcha.md)).
2. **Configuración → Comunicación NOC**: ajustar el plazo para informar, los turnos y las guías, y vincular la cuenta de WhatsApp con el QR ([Comunicación del NOC](docs/comunicacion-noc.md)).
3. **Una semana de observación** antes de publicar en el grupo del NOC: vincular Telegram y WhatsApp recién al terminarla (o tenerlos en pausa), revisar con la red real lo que ODIN asocia y aprobar los umbrales adaptativos que propone. La calibración de **Casos** muestra cuántos casos habría avisado cada día.
4. Activar los avisos de casos, vincular o reanudar los grupos y entregar la [Guía para N1](docs/guia-n1.md) a los operadores.

Los catálogos de servicios externos se cargan y activan solos al arrancar. Conviene revisarlos con los flujos del propio ISP ([Catálogos](docs/catalogos.md)).

## Usuarios y acceso

- **Administrador**: usuarios, fuentes, organización del monitoreo, guías, WhatsApp, turnos y umbrales.
- **Operador**: consulta todo, reconoce casos e incidentes, informa al grupo del NOC y escala a N2.

MFA acepta un código TOTP o un código de recuperación de un solo uso. Los códigos se renuevan desde *Mi acceso* (con contraseña y factor válido), lo que invalida todos los anteriores. Si se pierde el acceso, la recuperación se hace desde el host:

```bash
docker compose exec api python -m odin.cli reset-access nombre_usuario
```

Esto revoca las sesiones, borra el MFA y exige una contraseña nueva y un registro de MFA nuevo al ingresar. No hay recuperación por email. ODIN no deja desactivar la propia cuenta y siempre conserva al menos un administrador activo.

## Analista

Hay dos proveedores, uno activo a la vez, que el administrador elige en **Casos**:

- **Modelo local (llama.cpp)**: `llama-server` en la red propia, con sus redes permitidas. Los datos no salen.
- **DeepSeek (API)**: `https://api.deepseek.com`, con API key y modelo (`deepseek-chat` por defecto, o `deepseek-reasoner`). **Lo que se analiza sale de la red**: nombres de equipos, IPs, descripciones de puertos, extractos de logs y casos.

El analista recibe un paquete de evidencia etiquetado: logs, alertas, incidentes, trazas, caídas de PON, la lectura y el veredicto de ODIN, el impacto, las notas e informes del NOC y las correcciones de casos anteriores. Cada respuesta se valida: si cita evidencias que no existen, se repara una vez o se descarta. Las valoraciones del NOC (correcto, parcial, incorrecto, con la corrección) alimentan los análisis siguientes del mismo asunto.

## Telegram y WhatsApp

| Comando de Telegram | Dónde | Qué hace |
|---|---|---|
| `/vincular <código>` | Chat privado con el bot | Vincula la cuenta de Telegram con el usuario de ODIN (código en *Mi acceso*). |
| `/grupo` | Grupo o tema del NOC | Publica ahí los casos que avisan, su diagnóstico (con botones de valoración y reconocimiento), los informes del N1 y las entregas de turno. |
| `/proveedores` | Grupo interno de proveedores | Un hilo por cada falla de tránsito, transporte, IXP o peering. Al responder en el hilo con el ticket o la ETA, queda como nota del caso. |
| `/pregunta [1h\|6h\|24h\|7d] <pregunta>` | Grupo del NOC | Le pregunta al analista sobre la red. Responder al hilo de un caso pregunta sobre ese caso. |
| `/pausar` · `/reanudar` | Grupo del NOC | Detiene o reanuda los envíos. |

Solo administradores de ODIN con la cuenta vinculada configuran los grupos. **WhatsApp** se vincula escaneando un QR en ODIN y publica en un grupo de la cuenta lo mismo que el grupo del NOC de Telegram, solo de salida. Los mensajes son informes técnicos con etiqueta de estado y campos fijos. Detalles: [Comunicación del NOC](docs/comunicacion-noc.md).

## Seguridad y datos

- `.env` y `.secrets/` están fuera de Git. La **clave maestra** descifra MFA y credenciales, y de ella se deriva el token del servicio de WhatsApp: hay que respaldarla aparte de PostgreSQL y no rotarla con solo reemplazar el archivo.
- Contraseñas con Argon2id, secretos con Fernet, sesiones y códigos de recuperación guardados como hash. La auditoría no incluye secretos.
- Cookies opacas, HttpOnly, SameSite=Strict y Secure según el entorno. Las mutaciones exigen el Origin exacto y CSRF. La API no publica puerto al host y solo confía en `X-Forwarded-For` del proxy de la propia instalación.
- Cada fuente interna (LibreNMS, Zabbix, Graylog, llama.cpp) tiene sus **redes permitidas**: ODIN no se conecta a otra dirección aunque el DNS apunte a otro lado. Los servicios públicos (SmartOLT, DeepSeek, Telegram) solo se aceptan en direcciones públicas.
- El sondeador y el tracer corren solo con `NET_RAW`, con el sistema de archivos de solo lectura, sin red del host ni acceso a Docker.
- ODIN guarda de las ONUs solo datos técnicos: no guarda dirección, contacto, credenciales PPPoE ni IPs de gestión.
- Lo que se publica en Telegram, WhatsApp o DeepSeek sale de la red: nombres, direcciones y lecturas de los casos.

## Persistencia, actualizaciones y pruebas

PostgreSQL y la sesión de WhatsApp viven en volúmenes de Compose; `docker compose down` los conserva. **No usar `down -v` en una instalación con datos.** Las migraciones se aplican solas antes de que arranquen la API y los procesos. Antes de actualizar, respaldar PostgreSQL y la clave maestra ([Configuración](docs/configuracion.md)).

```bash
bash scripts/test.sh
```

Las pruebas corren en **otro proyecto de Compose**, con un PostgreSQL temporal `odin_test` sin puertos publicados, y el script elimina solo ese entorno. Cubren autenticación y permisos, la cola de trabajos, el sondeo y el detector, las trazas, los catálogos, los umbrales adaptativos, los veredictos, los casos de acceso, el impacto y la comunicación del NOC.

## Documentos

- [Guía para N1](docs/guia-n1.md): cómo trabaja un operador con ODIN durante el turno.
- [Puesta en marcha](docs/puesta-en-marcha.md): el asistente paso a paso, credenciales de solo lectura y convención de descripciones de puertos.
- [Arquitectura](docs/arquitectura.md): servicios, flujo de datos, recursos y conexiones salientes.
- [Configuración](docs/configuracion.md): variables de entorno, capacidad, DNS, actualizaciones, respaldos y problemas frecuentes.
- [Casos y veredictos](docs/casos-y-veredictos.md): reglas del correlador, veredicto de servicios externos, casos de acceso, impacto y qué avisa.
- [Organización del monitoreo](docs/organizacion-monitoreo.md): destinos, comprobaciones, perfiles, jitter y umbrales adaptativos.
- [Catálogos](docs/catalogos.md): los catálogos de servicios externos y cómo adaptarlos a cada ISP.
- [Trazas de ruta](docs/trazas-de-ruta.md): mtr, cambios de ruta, nombres de redes y trazas en los casos.
- [OLTs con SmartOLT](docs/olts-smartolt.md): inventario de OLTs, PON, uplinks y caídas, en solo lectura.
- [Comunicación del NOC](docs/comunicacion-noc.md): guías, informes, Telegram, WhatsApp, plazo y turnos.
- [Vigilancia de ODIN](docs/vigilancia-odin.md): cómo ODIN avisa de sus propias fallas.
- [Identidad visual](docs/identidad-visual.md)
