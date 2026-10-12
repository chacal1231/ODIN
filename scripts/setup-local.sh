#!/usr/bin/env bash
# Prepares a server for ODIN: installs Docker and Compose when missing, creates the master key and .env, asks how the
# panel is published and, optionally, starts ODIN and creates the first administrator.
# Safe to run again: secrets are never regenerated, other lines of .env are kept and current values are the defaults.
# Without a terminal it takes every default (or the ODIN_* variables already in the environment) and starts nothing.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

# shellcheck source=scripts/lib.sh
. scripts/lib.sh

say 'Requisitos'
require_docker

# The CDNs must answer ODIN as they answer the customers: see docs/configuracion.md.
resolvers=$( { command -v resolvectl >/dev/null && resolvectl dns 2>/dev/null; } || grep '^nameserver' /etc/resolv.conf || true)
if grep -qwE '8\.8\.8\.8|8\.8\.4\.4|1\.1\.1\.1|1\.0\.0\.1|9\.9\.9\.9' <<<"$resolvers"; then
  warn 'el servidor resuelve con DNS públicos; usa los recursivos del ISP (docs/configuracion.md, «DNS del servidor»)'
fi

say 'Secretos'
first_install=0
[[ -f .env ]] || { first_install=1; touch .env; }
mkdir -p .secrets
if [[ ! -f .secrets/master.key ]]; then
  if grep -q '^ODIN_DATABASE_URL=' .env; then
    fail '.env ya existe pero falta .secrets/master.key: restaura la clave maestra del respaldo (sin ella no se descifran las credenciales guardadas)'
  fi
  # Fernet key: 32 random bytes in URL-safe base64.
  head -c 32 /dev/urandom | base64 | tr '+/' '-_' > .secrets/master.key
  # Containers run as uid 10001; the parent remains 0700 and only this file is mounted.
  chmod 444 .secrets/master.key
  echo 'Clave maestra creada en .secrets/master.key. Respáldala fuera del servidor.'
else
  echo 'Clave maestra existente: se conserva.'
fi
password=$(current POSTGRES_PASSWORD '')
if [[ -z $password ]]; then
  password=$(random_hex 32)
  set_env POSTGRES_PASSWORD "$password"
  set_env ODIN_DATABASE_URL "postgresql+psycopg://odin:$password@db:5432/odin"
  echo 'Contraseña de PostgreSQL generada.'
else
  grep -q '^ODIN_DATABASE_URL=' .env || set_env ODIN_DATABASE_URL "postgresql+psycopg://odin:$password@db:5432/odin"
  echo 'Contraseña de PostgreSQL existente: se conserva.'
fi
set_env ODIN_MASTER_KEY_FILE /run/secrets/master.key

say 'Instalación'
ask isp_name 'Nombre del ISP, junto a ODIN en la cabecera (hasta 60 caracteres)' "$(current ODIN_ISP_NAME '')" '^[^#"\\]{0,60}$'
system_tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
# Servers usually run in UTC; the NOC's local time is what matters here.
case $system_tz in ''|UTC|Etc/UTC|Etc/Universal) system_tz=America/Bogota ;; esac
ask timezone 'Zona horaria de turnos y mensajes' "$(current ODIN_TIMEZONE "$system_tz")" '^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$'
[[ -e /usr/share/zoneinfo/$timezone ]] || warn "no se encontró la zona horaria $timezone en este servidor; revísala"
set_env ODIN_ISP_NAME "$isp_name"
set_env ODIN_TIMEZONE "$timezone"

say 'Publicación del panel'
echo '  1) Local: solo 127.0.0.1, se entra con un túnel SSH'
echo '  2) WAN: por la IP pública en un puerto propio, HTTPS con la CA interna de Caddy'
echo '  3) Dominio: dominio propio con los puertos 80 y 443 libres, certificado de Let'"'"'s Encrypt'
case $(current COMPOSE_FILE '') in
  *compose.wan.yaml*) mode=2 ;;
  *compose.public.yaml*) mode=3 ;;
  *) mode=1 ;;
esac
ask mode 'Opción' "$mode" '^[123]$'
case $mode in
  1)
    ask port 'Puerto local de la web' "$(current ODIN_WEB_PORT 8080)" '^[0-9]{2,5}$'
    set_env ODIN_WEB_PORT "$port"
    origin="http://localhost:$port"
    set_env ODIN_SECURE_COOKIES false
    set_env COMPOSE_FILE compose.yaml
    ;;
  2)
    detected=$(host_ip)
    ask wan_host 'IP pública del servidor' "$(current ODIN_WAN_HOST "$detected")" '^([0-9]{1,3}\.){3}[0-9]{1,3}$'
    ask port 'Puerto HTTPS' "$(current ODIN_WAN_PORT 8090)" '^[0-9]{2,5}$'
    ask allow 'Redes que pueden abrir el panel, separadas por espacio (vacío: todas)' "$(current ODIN_WAN_ALLOW '')" '^[0-9A-Fa-f.:/ ]*$'
    [[ -n $allow ]] || warn 'el panel quedará abierto a todo internet; Docker publica el puerto por encima de ufw'
    set_env ODIN_WAN_HOST "$wan_host"
    set_env ODIN_WAN_PORT "$port"
    set_env ODIN_WAN_ALLOW "$allow"
    origin="https://$wan_host:$port"
    set_env ODIN_SECURE_COOKIES true
    set_env COMPOSE_FILE compose.yaml:compose.wan.yaml
    ;;
  3)
    ask domain 'Dominio del panel (por ejemplo odin.miisp.com)' "$(current ODIN_DOMAIN '')" '^([a-z0-9-]+\.)+[a-z]{2,}$'
    set_env ODIN_DOMAIN "$domain"
    origin="https://$domain"
    set_env ODIN_SECURE_COOKIES true
    set_env COMPOSE_FILE compose.yaml:compose.public.yaml
    ;;
esac
set_env ODIN_ORIGIN "$origin"
echo ".env listo. Las demás variables (capacidad del sondeador, ASN, sesiones) están en docs/configuracion.md."

start_default=n
if (( INTERACTIVE )); then start_default=s; fi
ask start '¿Construir y levantar ODIN ahora? (s/n)' "$start_default" '^[sSnN]$'
if [[ $start == [sS] ]]; then
  # Every question before the build, so the rest runs unattended.
  admin_default=''
  if (( first_install )); then admin_default=nocadmin; fi
  ask admin 'Administrador a crear (vacío para omitir)' "$admin_default" '^([a-z0-9._-]{3,80})?$'
  mfa_flag=()
  if [[ -n $admin ]]; then
    ask_password admin_password "$admin (temporal: ODIN pide cambiarla en el primer ingreso)" 14
    ask admin_mfa '¿Exigirle 2FA (autenticador TOTP)? Recomendado si el panel sale por WAN o dominio (s/n)' n '^[sSnN]$'
    if [[ $admin_mfa == [sS] ]]; then mfa_flag=(--mfa); fi
  fi
  say 'Construyendo y levantando ODIN (la primera vez tarda varios minutos)'
  compose up --build -d
  if [[ -n $admin ]]; then
    # By stdin: the password never appears among the process arguments.
    printf '%s\n' "$admin_password" | compose exec -T api python -m odin.cli create-admin "$admin" --password-stdin "${mfa_flag[@]}"
    if (( SUGGESTED )); then echo "Contraseña temporal de $admin (la sugerida): $admin_password"; fi
  fi
else
  echo
  echo 'Para levantarlo:      docker compose up --build -d'
  echo 'Primer administrador: docker compose exec api python -m odin.cli create-admin nocadmin'
fi

say 'Acceso'
case $mode in
  1) echo "Abre $origin desde el servidor, o desde tu equipo con: ssh -L $port:localhost:$port <servidor>" ;;
  2) echo "Abre $origin. El navegador pedirá confiar una vez en el certificado de la CA interna de Caddy." ;;
  3) echo "Abre $origin. El registro DNS de $domain debe apuntar a este servidor y los puertos 80 y 443 deben llegar a él." ;;
esac
