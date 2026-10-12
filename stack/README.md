# Fuentes de ODIN: Graylog, Zabbix y LibreNMS

Para un ISP que no tiene alguna de las fuentes que lee ODIN. Si ya existen, no hace falta nada de esto: basta con darle a ODIN una credencial de solo lectura ([Puesta en marcha](../docs/puesta-en-marcha.md)).

Cada herramienta es independiente: su propio Compose, sus volúmenes y sus secretos. Se instala sola, en el servidor que se elija, y ODIN se conecta por la IP de ese servidor, igual que con las fuentes de un ISP que ya las tiene.

| Carpeta | Versión | Qué le da a ODIN | Puertos |
|---|---|---|---|
| [`librenms/`](librenms/) | LibreNMS 26.9 + MariaDB 11.4 + Redis | Equipos, puertos con su IP y descripción, alertas | Web y API: 8000 |
| [`zabbix/`](zabbix/) | Zabbix 7.0 LTS + PostgreSQL 17 | Hosts y sus problemas | Web y API: 8081 · agentes activos: 10051 |
| [`graylog/`](graylog/) | Graylog 7.1 + Data Node + MongoDB 7 | Logs de BGP, interfaces y BFD de los routers | Web y API: 9000 · syslog: 514/udp |

## Dónde instalarlas

| Herramienta | Mínimo orientativo |
|---|---|
| Graylog | 4 vCPU, 8 GB de RAM; disco según el volumen de logs (30 a 40 días de retención por defecto) |
| Zabbix | 2 vCPU, 4 GB de RAM |
| LibreNMS | 2 vCPU, 4 GB de RAM; el disco crece con los RRD de cada puerto |

- **Graylog** conviene en un servidor propio: OpenSearch, dentro del Data Node, es lo más pesado de todo el conjunto.
- **LibreNMS y Zabbix** pueden compartir servidor: sus puertos no chocan.
- **ODIN** necesita su propio servidor ([requisitos](../README.md#requisitos)). Juntar las cuatro en una sola máquina solo sirve para laboratorio.

## Instalación

En cada servidor, con el repositorio clonado:

```bash
bash stack/librenms/setup.sh
bash stack/zabbix/setup.sh
bash stack/graylog/setup.sh
```

Cada script:
1. **Requisitos:** instala Docker, Compose, `curl` y `jq` si faltan. Graylog además sube `vm.max_map_count` a 262144, que OpenSearch exige.
2. **Configuración:** pregunta la IP del servidor, los puertos y la zona horaria, y escribe `.env` con secretos generados.
3. **Arranque:** levanta los contenedores.
4. **Acceso para ODIN:** crea un usuario de solo lectura y su token:
   - LibreNMS: usuario `odin` con rol *global-read*.
   - Zabbix: usuario `odin` en el grupo *ODIN lectura*, con lectura sobre todos los grupos de hosts y sin acceso a la web.
   - Graylog: usuario `odin` con rol *Reader*, el stream por defecto compartido para ver y permiso de búsqueda.
5. **Administración:** deja la cuenta de administrador lista. LibreNMS y Graylog la crean sin pasar por el asistente web; en Zabbix se reemplaza la contraseña por defecto de `Admin`. La contraseña se puede escribir o dejar que el script la genere.
6. **Datos para ODIN:** muestra el endpoint, la red permitida y el token para **Configuración → Puesta en marcha**.

**El token se muestra una sola vez y no se guarda en el servidor.** Si se pierde, volver a ejecutar el script y pedir uno nuevo: el anterior queda revocado.

## Volver a ejecutar

Los scripts se pueden ejecutar de nuevo. Los secretos no se regeneran, los valores actuales son los propuestos y las demás líneas de `.env` se conservan. Si ODIN ya tiene su acceso, el script pregunta si se quiere un token nuevo. Para eso pide la contraseña del administrador en Graylog y Zabbix; LibreNMS no la necesita.

En **Zabbix**, los permisos de ODIN son sobre los grupos de hosts que existen al ejecutar el script. Después de crear grupos nuevos, volver a ejecutarlo y pedir la actualización: así ODIN los lee. Esa misma ejecución emite un token nuevo.

## Después de instalar

- **Routers → Graylog:** enviar el syslog a la IP del servidor, puerto 514/udp. El input *Routers (syslog UDP)* ya existe.
- **Equipos → LibreNMS:** agregarlos por su IP de gestión con SNMP. ODIN saca a los proveedores de **la descripción de cada puerto** ([cómo](../docs/puesta-en-marcha.md#paso-2-cómo-reconoce-odin-a-los-proveedores)). La comunidad SNMP que se indicó al instalar es la que LibreNMS prueba por defecto.
- **Alertas → LibreNMS:** crear las reglas en *Alerts → Alert Rules* (por ejemplo, desde la colección incluida). ODIN lee las alertas que generan.
- **Zabbix:** agregar los hosts, con SNMP o con agente, en los grupos de hosts que correspondan.

## Seguridad

- **Puertos:** cada servicio publica sus puertos solo en la IP elegida. Las bases de datos, MongoDB, OpenSearch y Redis no se publican.
- **Firewall:** Docker publica esos puertos por encima de `ufw`. Para limitar quién llega, filtrar en la cadena `DOCKER-USER` de iptables o en el firewall de la red:
  - la web y la API, desde el NOC y desde ODIN;
  - el syslog, desde los routers;
  - el 10051 de Zabbix, desde los agentes.
- **HTTP sin cifrar:** las APIs van por HTTP y deben quedar dentro de la red de gestión. Los tokens de ODIN son de solo lectura.
- **Contraseñas guardadas:** las de las bases de datos están en `.env`, con permisos `0600`, igual que el secreto de Graylog. De la contraseña de admin de Graylog solo se guarda su hash SHA-256, que es lo que Graylog exige.

## Operación

Desde la carpeta de cada herramienta:

```bash
docker compose ps
docker compose logs -f --tail=100
docker compose pull && docker compose up -d     # después de cambiar la versión en compose.yaml
```

Las versiones están fijadas en cada `compose.yaml`. Antes de actualizar, respaldar los volúmenes (`docker compose down` los conserva; **no usar `down -v`**). En Graylog no se puede saltar versiones mayores, y en Zabbix el salto de LTS migra la base de datos al arrancar.
