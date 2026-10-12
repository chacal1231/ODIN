#!/usr/bin/env bash
# Installs Graylog ready for ODIN: secrets and .env, the Data Node's first configuration (certificates), a syslog UDP input
# for the routers and a read-only user whose token ODIN uses. Prints what to enter in ODIN → Puesta en marcha → Graylog.
# Safe to run again: secrets are kept, and on request it issues a new token for ODIN (the old one is revoked).
set -euo pipefail
cd "$(dirname "$0")"
umask 077
# shellcheck source=../../scripts/lib.sh
. ../../scripts/lib.sh

INPUT_TITLE='Routers (syslog UDP)'
DEFAULT_STREAM=000000000000000000000001

say 'Requisitos'
require_docker
require_tools curl jq
memory=$(awk '/MemTotal/ { print int($2 / 1048576) }' /proc/meminfo)
(( memory >= 8 )) || warn "este servidor tiene ${memory} GB de RAM; Graylog con Data Node necesita al menos 8 GB"
# OpenSearch (inside the Data Node) refuses to start below this.
if (( $(sysctl -n vm.max_map_count) < 262144 )); then
  echo 'Subiendo vm.max_map_count a 262144 (requisito de OpenSearch).'
  $SUDO sysctl -qw vm.max_map_count=262144
  echo 'vm.max_map_count = 262144' | $SUDO tee /etc/sysctl.d/99-graylog-datanode.conf >/dev/null
fi

say 'Configuración'
[[ -f .env ]] || touch .env
ask bind_ip 'IP de este servidor (la usan ODIN, los routers y el navegador)' "$(current BIND_IP "$(host_ip)")" '^([0-9]{1,3}\.){3}[0-9]{1,3}$'
[[ $bind_ip != 127.* ]] || fail 'ODIN no se conecta a direcciones de loopback: usa la IP de la red'
ask port 'Puerto de la web y la API' "$(current GRAYLOG_PORT 9000)" '^[0-9]{2,5}$'
ask syslog_port 'Puerto UDP para el syslog de los routers' "$(current SYSLOG_PORT 514)" '^[0-9]{2,5}$'
ask tz 'Zona horaria' "$(current TZ America/Bogota)" '^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$'
set_env BIND_IP "$bind_ip"
set_env GRAYLOG_PORT "$port"
set_env SYSLOG_PORT "$syslog_port"
set_env TZ "$tz"
[[ -n $(current GRAYLOG_PASSWORD_SECRET '') ]] || set_env GRAYLOG_PASSWORD_SECRET "$(random_password 96)"
URL="http://$bind_ip:$port"

provisioned=$(current ODIN_PROVISIONED '')
renew=n
if [[ $provisioned == yes ]]; then ask renew '¿Emitir un token nuevo para ODIN? El anterior deja de servir (s/n)' n '^[sSnN]$'; fi
password=''
if [[ -z $(current GRAYLOG_ROOT_PASSWORD_SHA2 '') ]]; then
  ask_password password 'el usuario admin de Graylog'
  generated=$SUGGESTED
  set_env GRAYLOG_ROOT_PASSWORD_SHA2 "$(printf '%s' "$password" | sha256sum | cut -d' ' -f1)"
elif [[ $provisioned != yes || $renew == [sS] ]]; then
  ask_secret password 'Contraseña actual del usuario admin de Graylog'
  [[ -n $password ]] || fail 'se necesita la contraseña de admin para preparar el acceso de ODIN'
fi

say 'Levantando Graylog'
compose up -d

api() {  # api METHOD PATH [JSON]
  local args=(-sS --fail-with-body -u "admin:$password" -H 'X-Requested-By: odin-setup' -H 'Accept: application/json' -X "$1")
  if [[ -n ${3:-} ]]; then args+=(-H 'Content-Type: application/json' --data "$3"); fi
  curl "${args[@]}" "$URL/api$2"
}

ready() { [[ -n $password ]] && curl -sf -u "admin:$password" -H 'Accept: application/json' "$URL/api/system" >/dev/null; }

# On its first start Graylog waits in "preflight" for the Data Node's certificates; this answers it as the web
# wizard would: a self-signed CA, automatic renewal, certificates for every Data Node, then resume.
preflight() {
  local initial
  initial=$(compose logs graylog 2>/dev/null | grep -o "password '[^']*'" | tail -1 | cut -d"'" -f2)
  [[ -n $initial ]] || return 1
  pf() {
    local args=(-sS --fail-with-body -u "admin:$initial" -H 'X-Requested-By: odin-setup' -H 'Content-Type: application/json' -X "$1")
    if [[ -n ${3:-} ]]; then args+=(--data "$3"); fi
    curl "${args[@]}" "$URL/api$2"
  }
  local nodes
  nodes=$(pf GET /data_nodes 2>/dev/null) || return 1
  if [[ -z $(pf GET /ca 2>/dev/null) ]]; then
    echo 'Primera configuración: creando la CA del Data Node.'
    pf POST /ca/create '{"organization":"Graylog CA"}' >/dev/null
    pf POST /renewal_policy '{"mode":"AUTOMATIC","certificate_lifetime":"P30D"}' >/dev/null
    pf POST /generate >/dev/null
    return 1
  fi
  # A Data Node that registers after the CA was created got no certificate: ask again while one waits.
  if jq -e 'any(.status == "UNCONFIGURED")' <<<"$nodes" >/dev/null; then
    echo 'Data Node sin certificado: emitiéndolo.'
    pf POST /generate >/dev/null
    return 1
  fi
  if jq -e 'length > 0 and all(.status == "CONNECTED")' <<<"$nodes" >/dev/null; then
    echo 'Data Node conectado: terminando la primera configuración.'
    pf POST /status/finish-config >/dev/null
  fi
  return 1
}

if [[ -n $password ]]; then
  waited=0
  printf 'Esperando a Graylog (el primer arranque tarda unos minutos)'
  until ready; do
    preflight || true
    (( waited < 900 )) || { echo; fail "Graylog no respondió en 15 minutos: revisa 'docker compose logs graylog datanode'"; }
    printf '.'
    sleep 5
    waited=$(( waited + 5 ))
  done
  echo ' listo.'
fi

token=''
if [[ $provisioned != yes || $renew == [sS] ]]; then
  say 'Preparando el acceso de ODIN'
  if ! api GET /system/inputs | jq -e --arg t "$INPUT_TITLE" '.inputs[] | select(.title == $t)' >/dev/null; then
    api POST /system/inputs "$(jq -nc --arg t "$INPUT_TITLE" '{title: $t, type: "org.graylog2.inputs.syslog.udp.SyslogUDPInput", global: true,
      configuration: {bind_address: "0.0.0.0", port: 1514, recv_buffer_size: 262144, number_worker_threads: 2, override_source: null,
      force_rdns: false, allow_override_date: true, store_full_message: true, expand_structured_data: false, timezone: "NotSet"}}')" >/dev/null
    echo "Input «$INPUT_TITLE» creado."
  fi
  if ! user_id=$(api GET /users/odin 2>/dev/null | jq -er .id); then
    api POST /users "$(jq -nc --arg p "$(random_password 32)" '{username: "odin", password: $p, email: "odin@localhost",
      first_name: "ODIN", last_name: "lectura", roles: ["Reader"], permissions: []}')" >/dev/null
    user_id=$(api GET /users/odin | jq -er .id)
    echo 'Usuario «odin» creado con el rol Reader.'
  fi
  # Reader does not include the classic search API that ODIN reads; the stream is shared for viewing only.
  api PUT /users/odin/permissions '{"permissions":["searches:relative"]}' >/dev/null
  api POST "/authz/shares/entities/grn::::stream:$DEFAULT_STREAM" \
    "$(jq -nc --arg g "grn::::user:$user_id" '{selected_grantee_capabilities: {($g): "view"}}')" >/dev/null
  for old in $(api GET "/users/$user_id/tokens" | jq -r '.tokens[] | select(.name == "odin") | .id'); do
    api DELETE "/users/$user_id/tokens/$old" >/dev/null
  done
  token=$(api POST "/users/$user_id/tokens/odin" '{"token_ttl":"P3650D"}' | jq -er .token)
  set_env ODIN_PROVISIONED yes
fi

say 'Datos para ODIN (Puesta en marcha → Graylog)'
echo "  Endpoint:          $URL"
echo "  Redes permitidas:  $bind_ip/32"
if [[ -n $token ]]; then echo "  Token:             $token"; echo '  (Se muestra solo esta vez; pégalo ahora en ODIN.)'
else echo '  Token:             ya emitido; vuelve a ejecutar ./setup.sh para emitir uno nuevo.'; fi
echo
echo "Configura los routers para enviar syslog a $bind_ip, puerto UDP $syslog_port."
echo "Panel de Graylog: $URL (usuario admin)"
if [[ ${generated:-0} == 1 ]]; then echo "Contraseña de admin (la sugerida): $password  (guárdala: no se vuelve a mostrar)"; fi
