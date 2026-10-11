# ODIN · Edición ISP

Consola de NOC para ISPs, instalada una por operador. Integra **Zabbix** y **LibreNMS** (inventario y eventos), **Graylog** (logs), sondeos ICMP/TCP propios hacia la red y hacia servicios externos, un mapa de POPs y equipos, casos correlacionados y un analista LLM que explica cada caso y responde preguntas sobre la red citando los datos.

## Inicio local

Requisitos: Docker Engine con Compose y Python 3 para generar secretos. No se necesita Node ni Python del backend instalados en el host.

```bash
bash scripts/setup-local.sh
docker compose up --build -d
docker compose exec api python -m odin.cli create-admin nocadmin
```

Abrir **http://localhost:8080**. La CLI solicita una contraseña temporal sin mostrarla; el primer ingreso exige contraseña nueva de al menos 14 caracteres y registro de MFA en un autenticador TOTP. Introducir manualmente la clave mostrada. Guardar los ocho códigos de recuperación antes de cerrar el aviso. No existen usuarios ni contraseñas predeterminadas.

El puerto web solo escucha en loopback. Para acceso desde otra máquina, usar un túnel SSH que conserve `localhost:8080` como origen. Esta configuración es de desarrollo. Para publicar con HTTPS, usar `compose.public.yaml` y `deploy/Caddyfile.public` cambiando `odin.example.com` por el dominio propio; no publicar el Compose de desarrollo tal cual ni desactivar Secure para un dominio público.

## Administración

El administrador puede crear usuarios (contraseña temporal mostrada una vez), revocar sesiones, desactivar usuarios y guardar configuración cifrada de fuentes. Cada fuente se prueba desde Administración después de configurar sus redes permitidas; hasta entonces aparece como **sin validar**. Zabbix y LibreNMS importan inventario y eventos; Graylog y el analista LLM tienen sus lectores y controles en Eventos y Casos. El operador puede consultar el panel pero no administrar usuarios ni integraciones.

MFA permite código TOTP o código de recuperación de un solo uso. No reutilizar el mismo TOTP en dos accesos dentro del mismo intervalo. La renovación de códigos requiere contraseña y factor válido; invalida todos los anteriores. La renovación está disponible para todos los usuarios desde su nombre en la cabecera → Mi acceso. Recuperación local cuando se pierde acceso:

```bash
docker compose exec api python -m odin.cli reset-access nombre_usuario
```

Este procedimiento requiere acceso al host, revoca sesiones/desafíos y exige nuevo registro MFA y contraseña al ingresar. No se ofrece recuperación por email. Se bloquea la desactivación de la propia cuenta y se conserva al menos un administrador activo.

## Persistencia y configuración

`.env` y `.secrets/` están excluidos de Git. El script no sobrescribe secretos existentes. PostgreSQL usa un volumen de Compose; `docker compose down` conserva datos. **No usar `down -v` en el entorno con datos que quieras conservar.** La clave maestra es necesaria para descifrar MFA y credenciales; se debe respaldar por separado de PostgreSQL. No rotarla reemplazando simplemente el archivo.

Migraciones se aplican en un proceso previo al API/worker. El backend falla si falta una clave válida o si se pretende usar cookies inseguras fuera de localhost. Las cookies son opacas, HttpOnly, SameSite=Strict y Secure según entorno. Las mutaciones exigen Origin exacto y CSRF. API no publica puerto al host ni confía en X-Forwarded-For. En E1 el límite por IP detrás del proxy es conservador y compartido; antes de publicación se debe configurar confianza explícita en el proxy para distinguir clientes sin aceptar cabeceras falsificadas.

Contraseñas con Argon2id, secretos con Fernet, sesiones y códigos de recuperación almacenados como hashes. Auditoría no incluye secretos. Dependencias Python fijadas en `backend/requirements.txt`; frontend con versiones exactas y `package-lock.json`.

## Verificación

```bash
bash scripts/test.sh
```

Las pruebas usan **otro proyecto Compose**, PostgreSQL temporal `odin_test` sin puertos publicados; el script elimina solamente ese entorno temporal. Cubren MFA, replay, recuperación, roles, CSRF, revocación, expiración, cifrado, rate limiting y reclamación concurrente de trabajos.

```bash
docker compose build web
docker compose ps
docker compose logs --tail=50 api worker analyst
```

La cola admite idempotencia, lease con heartbeat, recuperación tras expiración, límite de intentos y fencing de resultados antiguos. E1 implementa un handler `healthcheck`; otros tipos fallan de forma acotada. No hay endpoints públicos para ejecutar trabajos arbitrarios.

## Documentos

- [Organización del monitoreo](docs/organizacion-monitoreo.md)
- [Catálogo de hyperscalers](docs/catalogo-hyperscalers.md)
- [Vigilancia de ODIN](docs/vigilancia-odin.md): el sondeador, los lectores de eventos y la cola se vigilan a sí mismos y los avisos aparecen en la barra de estado.
- [Trazas de ruta](docs/trazas-de-ruta.md): mtr hacia cada destino cada 15 minutos y en cada incidente, con cada salto leído contra el inventario.
- [Identidad visual](docs/identidad-visual.md)

### Organización del monitoreo

El panel separa **Red propia**, **Servicios externos** e **Incidentes**. Cada destino admite varias comprobaciones con historial independiente. Consulta [Organización del monitoreo](docs/organizacion-monitoreo.md) para jerarquías, perfiles, asociaciones con el mapa y alcance de los estados agregados.
