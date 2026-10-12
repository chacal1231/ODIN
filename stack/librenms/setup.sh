#!/usr/bin/env bash
# Installs LibreNMS ready for ODIN: secrets and .env, the administrator (skipping the web wizard), and a global-read
# user whose API token ODIN uses. Prints what to enter in ODIN → Puesta en marcha → LibreNMS.
# Safe to run again: secrets are kept, and on request it issues a new token for ODIN (the old one is revoked).
set -euo pipefail
cd "$(dirname "$0")"
umask 077
# shellcheck source=../../scripts/lib.sh
. ../../scripts/lib.sh

say 'Requisitos'
require_docker
require_tools curl

say 'Configuración'
[[ -f .env ]] || touch .env
ask bind_ip 'IP de este servidor (la usan ODIN y el navegador)' "$(current BIND_IP "$(host_ip)")" '^([0-9]{1,3}\.){3}[0-9]{1,3}$'
[[ $bind_ip != 127.* ]] || fail 'ODIN no se conecta a direcciones de loopback: usa la IP de la red'
ask port 'Puerto de la web y la API' "$(current LIBRENMS_PORT 8000)" '^[0-9]{2,5}$'
ask tz 'Zona horaria (también la de los eventos en ODIN)' "$(current TZ America/Bogota)" '^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$'
ask community 'Comunidad SNMP v2c por defecto de los equipos' "$(current SNMP_COMMUNITY public)" '^[^#"\\ ]{1,64}$'
set_env BIND_IP "$bind_ip"
set_env LIBRENMS_PORT "$port"
set_env TZ "$tz"
set_env SNMP_COMMUNITY "$community"
[[ -n $(current DB_PASSWORD '') ]] || set_env DB_PASSWORD "$(random_hex 32)"
URL="http://$bind_ip:$port"

provisioned=$(current ODIN_PROVISIONED '')
renew=n
if [[ $provisioned == yes ]]; then ask renew '¿Emitir un token nuevo para ODIN? El anterior deja de servir (s/n)' n '^[sSnN]$'; fi

say 'Levantando LibreNMS'
compose up -d

# lnms SUBCOMMAND ARGS...: the image's wrapper takes options only after the subcommand.
lnms() { local command=$1; shift; compose exec -T --user librenms librenms lnms "$command" --no-ansi -n "$@"; }
user_exists() {
  [[ $(compose exec -T -e MYSQL_PWD="$(current DB_PASSWORD '')" db mariadb -ulibrenms librenms -Nse \
    "SELECT COUNT(*) FROM users WHERE username = '$1'" 2>/dev/null) == 1 ]]
}
# The web answers once the image has applied the database migrations.
web_up() { [[ $(curl -s -o /dev/null -w '%{http_code}' "$URL/login") =~ ^(200|302)$ ]]; }
wait_until 600 'a LibreNMS' web_up

token='' admin_password='' generated=0
if [[ $provisioned != yes || $renew == [sS] ]]; then
  say 'Preparando el acceso de ODIN'
  if ! user_exists admin; then
    ask_secret admin_password 'Contraseña para el usuario admin de LibreNMS (vacío: generar una)'
    if [[ -z $admin_password ]]; then admin_password=$(random_password 24); generated=1; fi
    lnms user:add -r admin -p "$admin_password" admin >/dev/null
    echo 'Usuario «admin» creado.'
    # On an empty database the image enables the web wizard; recreated on a populated one, it does not.
    compose up -d --force-recreate librenms >/dev/null 2>&1
    wait_until 600 'a LibreNMS' web_up
  fi
  if ! user_exists odin; then
    lnms user:add -r global-read -p "$(random_password 32)" -l 'ODIN lectura' odin >/dev/null
    echo 'Usuario «odin» creado con el rol global-read.'
  fi
  for old in $(lnms api:token-list odin | awk -F'|' '$3 ~ /^ *odin *$/ { gsub(/ /, "", $2); print $2 }'); do
    lnms api:token-revoke odin "$old" >/dev/null
  done
  token=$(lnms api:token-create --name odin odin | grep -oE '^[0-9]+\|[A-Za-z0-9]+' | head -1)
  [[ -n $token ]] || fail 'LibreNMS no devolvió el token'
  set_env ODIN_PROVISIONED yes
fi

say 'Datos para ODIN (Puesta en marcha → LibreNMS)'
echo "  Endpoint:          $URL"
echo "  Redes permitidas:  $bind_ip/32"
echo "  Zona horaria:      $tz  (la de los eventos de LibreNMS)"
if [[ -n $token ]]; then echo "  Token:             $token"; echo '  (Se muestra solo esta vez; pégalo ahora en ODIN.)'
else echo '  Token:             ya emitido; vuelve a ejecutar ./setup.sh para emitir uno nuevo.'; fi
echo
echo "Panel de LibreNMS: $URL (usuario admin)"
if (( generated )); then echo "Contraseña de admin generada: $admin_password  (guárdala: no se vuelve a mostrar)"; fi
echo 'Agrega los equipos (Devices → Add Device) con la IP de gestión y SNMP; ODIN lee las descripciones de sus puertos.'
