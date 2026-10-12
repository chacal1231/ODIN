#!/usr/bin/env bash
# Installs Zabbix 7.0 LTS ready for ODIN: secrets and .env, a user group with read rights on every host group, a read-only
# user whose API token ODIN uses, and a new password for Admin in place of the default one. Prints what to enter in
# ODIN → Puesta en marcha → Zabbix.
# Safe to run again: on request it refreshes the read rights with the host groups created since and issues a new token.
set -euo pipefail
cd "$(dirname "$0")"
umask 077
# shellcheck source=../../scripts/lib.sh
. ../../scripts/lib.sh

GROUP='ODIN lectura'

say 'Requisitos'
require_docker
require_tools curl jq

say 'Configuración'
[[ -f .env ]] || touch .env
ask bind_ip 'IP de este servidor (la usan ODIN, los agentes y el navegador)' "$(current BIND_IP "$(host_ip)")" '^([0-9]{1,3}\.){3}[0-9]{1,3}$'
[[ $bind_ip != 127.* ]] || fail 'ODIN no se conecta a direcciones de loopback: usa la IP de la red'
ask port 'Puerto de la web y la API' "$(current ZABBIX_WEB_PORT 8081)" '^[0-9]{2,5}$'
ask server_port 'Puerto del servidor para agentes activos' "$(current ZABBIX_SERVER_PORT 10051)" '^[0-9]{2,5}$'
ask tz 'Zona horaria' "$(current TZ America/Bogota)" '^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$'
set_env BIND_IP "$bind_ip"
set_env ZABBIX_WEB_PORT "$port"
set_env ZABBIX_SERVER_PORT "$server_port"
set_env TZ "$tz"
[[ -n $(current POSTGRES_PASSWORD '') ]] || set_env POSTGRES_PASSWORD "$(random_hex 32)"
URL="http://$bind_ip:$port"

provisioned=$(current ODIN_PROVISIONED '')
renew=n
if [[ $provisioned == yes ]]; then
  ask renew '¿Actualizar los permisos de ODIN con los grupos de hosts nuevos y emitir un token nuevo? (s/n)' n '^[sSnN]$'
fi

admin_password='' generated=0
if [[ $provisioned != yes ]]; then
  # Replaces Zabbix's default password; unused if Admin already has another.
  ask_password admin_password 'el usuario Admin de Zabbix'
  generated=$SUGGESTED
fi

say 'Levantando Zabbix'
compose up -d

rpc() {  # rpc METHOD PARAMS_JSON [AUTH]; prints .result, fails with Zabbix's message.
  local headers=(-H 'Content-Type: application/json-rpc') reply
  if [[ -n ${3:-} ]]; then headers+=(-H "Authorization: Bearer $3"); fi
  # Explicit returns: inside `until` and `if` conditions bash ignores set -e.
  reply=$(curl -sS "${headers[@]}" --data "$(jq -nc --arg m "$1" --argjson p "$2" '{jsonrpc: "2.0", method: $m, params: $p, id: 1}')" \
    "$URL/api_jsonrpc.php") || return 1
  jq -e 'type == "object"' <<<"$reply" >/dev/null 2>&1 || return 1
  if jq -e '.error' <<<"$reply" >/dev/null; then jq -r '"\(.error.message) \(.error.data)"' <<<"$reply" >&2; return 1; fi
  jq -c '.result' <<<"$reply"
}
login() { rpc user.login "$(jq -nc --arg p "$1" '{username: "Admin", password: $p}')" | jq -r .; }

# The API answers before the server has created the database schema; a login attempt (right or wrong) needs both.
schema_ready() {
  local reply
  reply=$(curl -sf -H 'Content-Type: application/json-rpc' \
    --data '{"jsonrpc":"2.0","method":"user.login","params":{"username":"Admin","password":"zabbix"},"id":1}' "$URL/api_jsonrpc.php") || return 1
  jq -e '.result or (.error.code == -32500)' <<<"$reply" >/dev/null
}
wait_until 600 'a Zabbix' schema_ready

token=''
if [[ $provisioned != yes || $renew == [sS] ]]; then
  say 'Preparando el acceso de ODIN'
  default_password=0
  if session=$(login zabbix 2>/dev/null); then
    default_password=1
  else
    ask_secret current_password 'Contraseña actual del usuario Admin de Zabbix'
    session=$(login "$current_password") || fail 'no se pudo entrar como Admin'
  fi

  # Read (2) on every host group that exists now; groups created later need this run again.
  rights=$(rpc hostgroup.get '{"output":["groupid"]}' "$session" | jq -c '[.[] | {id: .groupid, permission: 2}]')
  group_id=$(rpc usergroup.get "$(jq -nc --arg n "$GROUP" '{output: ["usrgrpid"], filter: {name: $n}}')" "$session" | jq -r '.[0].usrgrpid // empty')
  if [[ -z $group_id ]]; then
    # gui_access 3: the group cannot open the web interface, only the API.
    group_id=$(rpc usergroup.create "$(jq -nc --arg n "$GROUP" --argjson r "$rights" '{name: $n, gui_access: 3, hostgroup_rights: $r}')" \
      "$session" | jq -r '.usrgrpids[0]')
    echo "Grupo «$GROUP» creado."
  else
    rpc usergroup.update "$(jq -nc --arg g "$group_id" --argjson r "$rights" '{usrgrpid: $g, hostgroup_rights: $r}')" "$session" >/dev/null
  fi
  echo "Lectura sobre $(jq length <<<"$rights") grupos de hosts."

  user_id=$(rpc user.get '{"output":["userid"],"filter":{"username":"odin"}}' "$session" | jq -r '.[0].userid // empty')
  if [[ -z $user_id ]]; then
    role_id=$(rpc role.get '{"output":["roleid"],"filter":{"name":"User role"}}' "$session" | jq -r '.[0].roleid')
    user_id=$(rpc user.create "$(jq -nc --arg p "$(random_password 32)" --arg g "$group_id" --arg r "$role_id" \
      '{username: "odin", name: "ODIN", surname: "lectura", passwd: $p, roleid: $r, usrgrps: [{usrgrpid: $g}]}')" "$session" | jq -r '.userids[0]')
    echo 'Usuario «odin» creado (rol User, sin acceso a la web).'
  fi

  old=$(rpc token.get "$(jq -nc --arg u "$user_id" '{output: ["tokenid"], userids: [$u], filter: {name: "odin"}}')" "$session" | jq -c '[.[].tokenid]')
  if [[ $old != '[]' ]]; then rpc token.delete "$old" "$session" >/dev/null; fi
  token_id=$(rpc token.create "$(jq -nc --arg u "$user_id" '{name: "odin", userid: $u, expires_at: "0"}')" "$session" | jq -r '.tokenids[0]')
  token=$(rpc token.generate "[\"$token_id\"]" "$session" | jq -r '.[0].token')

  # Last, so an interrupted run can be repeated with the default password.
  if (( default_password )); then
    [[ -n $admin_password ]] || { ask_password admin_password 'el usuario Admin de Zabbix'; generated=$SUGGESTED; }
    until rpc user.update "$(jq -nc --arg p "$admin_password" '{userid: "1", current_passwd: "zabbix", passwd: $p}')" "$session" >/dev/null; do
      (( INTERACTIVE )) || fail 'Zabbix rechazó la contraseña nueva de Admin'
      echo '  Zabbix rechazó esa contraseña; elige otra.'
      ask_password admin_password 'el usuario Admin de Zabbix'
      generated=$SUGGESTED
    done
    echo 'Contraseña de Admin cambiada.'
  else
    generated=0
  fi
  set_env ODIN_PROVISIONED yes
fi

say 'Datos para ODIN (Puesta en marcha → Zabbix)'
echo "  Endpoint:          $URL"
echo "  Redes permitidas:  $bind_ip/32"
if [[ -n $token ]]; then echo "  Token:             $token"; echo '  (Se muestra solo esta vez; pégalo ahora en ODIN.)'
else echo '  Token:             ya emitido; vuelve a ejecutar ./setup.sh para emitir uno nuevo.'; fi
echo
echo "Panel de Zabbix: $URL (usuario Admin)"
if (( generated )); then echo "Contraseña de Admin (la sugerida): $admin_password  (guárdala: no se vuelve a mostrar)"; fi
echo "Agentes activos: servidor $bind_ip, puerto $server_port."
echo "Al crear grupos de hosts nuevos, vuelve a ejecutar ./setup.sh para que ODIN los lea."
