# Shared by ODIN's installer and the stack/ installers: prompts, .env editing and host requirements.
# Every function works on .env in the current directory. Sourced, never run.

SUDO=''
if [[ $EUID -ne 0 ]]; then SUDO='sudo'; fi
INTERACTIVE=0
if [[ -t 0 ]]; then INTERACTIVE=1; fi
# Compose v2.24 is the first to understand the `!override` tag of ODIN's publication files.
COMPOSE_MIN=2.24.0
DOCKER=(docker)

say() { printf '\n== %s\n' "$*"; }
warn() { printf 'Aviso: %s\n' "$*" >&2; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# Value in .env, else in the environment, else the default.
current() {
  local value=''
  if [[ -f .env ]]; then value=$(grep -m1 "^$1=" .env | cut -d= -f2- || true); fi
  if [[ -z $value ]]; then value=${!1:-}; fi
  printf '%s' "${value:-$2}"
}

# ask VAR "Pregunta" default [regex]
# Locals carry a prefix: `printf -v` would assign a local of the same name instead of the caller's variable.
ask() {
  local _var=$1 _question=$2 _default=$3 _pattern=${4:-} _answer
  while true; do
    _answer=''
    if (( INTERACTIVE )); then read -rp "$_question${_default:+ [$_default]}: " _answer || INTERACTIVE=0; fi
    _answer=${_answer:-$_default}
    if [[ -z $_pattern || $_answer =~ $_pattern ]]; then printf -v "$_var" '%s' "$_answer"; return; fi
    (( INTERACTIVE )) || fail "valor no válido para «$_question»: $_answer"
    echo '  Valor no válido.'
  done
}

# ask_secret VAR "Pregunta": hidden input; empty when there is no terminal.
ask_secret() {
  local _answer=''
  if (( INTERACTIVE )); then read -rsp "$2: " _answer || true; echo; fi
  printf -v "$1" '%s' "$_answer"
}

# ask_password VAR "para quién" [min]: shows a random suggestion; Enter keeps it, or the person types their own twice
# (hidden). SUGGESTED is 1 when the suggestion was kept. Without a terminal, the suggestion is used.
ask_password() {
  local _var=$1 _who=$2 _min=${3:-12} _password _own _again
  _password=$(random_password 20)
  SUGGESTED=1
  if (( INTERACTIVE )); then
    echo "Contraseña sugerida para $_who: $_password"
    while true; do
      _own=''
      read -rsp '  Enter para usarla, o escribe otra: ' _own || true
      echo
      [[ -n $_own ]] || break
      if (( ${#_own} < _min )); then echo "  Mínimo $_min caracteres."; continue; fi
      _again=''
      read -rsp '  Repítela: ' _again || true
      echo
      if [[ $_own != "$_again" ]]; then echo '  No coinciden.'; continue; fi
      _password=$_own
      SUGGESTED=0
      break
    done
  fi
  printf -v "$_var" '%s' "$_password"
}

# Replaces KEY's line in .env, or appends it; every other line stays as it is.
set_env() {
  local tmp
  tmp=$(mktemp .env.XXXXXX)
  K=$1 V=$2 awk 'index($0, ENVIRON["K"] "=") == 1 { print ENVIRON["K"] "=" ENVIRON["V"]; done = 1; next }
                 { print } END { if (!done) print ENVIRON["K"] "=" ENVIRON["V"] }' .env > "$tmp"
  mv "$tmp" .env
}

random_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
# Letters and digits only: safe in URLs, shells and every tool's password policy.
random_password() {
  # No `head` after `tr`: under pipefail its SIGPIPE would abort the caller.
  local chars
  chars=$(head -c 512 /dev/urandom | tr -dc 'A-Za-z0-9')
  printf '%s' "${chars:0:${1:-24}}"
}

# The address other servers use to reach this one.
host_ip() { ip -4 route get 1.1.1.1 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1) }'; }

# wait_until SECONDS "qué" command...: retries every 5 s.
wait_until() {
  local limit=$1 what=$2 waited=0
  shift 2
  printf 'Esperando %s' "$what"
  until "$@" >/dev/null 2>&1; do
    (( waited < limit )) || { echo; fail "$what no respondió en $limit s"; }
    printf '.'
    sleep 5
    waited=$(( waited + 5 ))
  done
  echo ' listo.'
}

apt_install() {
  [[ -z $SUDO ]] || command -v sudo >/dev/null || fail 'falta sudo: ejecuta el script como root'
  # shellcheck disable=SC1091
  . /etc/os-release
  case " ${ID:-} ${ID_LIKE:-} " in
    *' ubuntu '*|*' debian '*) ;;
    *) fail "instala $* y vuelve a ejecutar este script" ;;
  esac
  say "Instalando $*"
  $SUDO apt-get update
  $SUDO apt-get install -y "$@"
}

# require_tools curl jq ...: installs what is missing.
require_tools() {
  local missing=() tool
  for tool in "$@"; do command -v "$tool" >/dev/null || missing+=("$tool"); done
  if (( ${#missing[@]} )); then apt_install "${missing[@]}"; fi
}

# Docker Engine and Compose v2; sets DOCKER to the command that reaches the daemon.
require_docker() {
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
      *' ubuntu '*) apt_install docker.io docker-compose-v2 ;;
      *' debian '*) apt_install docker.io docker-compose ;;
      *) fail 'instala Docker Engine y Compose v2 (https://docs.docker.com/engine/install/) y vuelve a ejecutar este script' ;;
    esac
    $SUDO systemctl enable --now docker
  fi
  DOCKER=(docker)
  if ! docker info >/dev/null 2>&1; then
    # Docker group membership equals root on the host: use sudo instead of adding the user to it.
    if [[ -n $SUDO ]] && $SUDO docker info >/dev/null 2>&1; then DOCKER=(sudo docker)
    else $SUDO systemctl start docker && DOCKER=($SUDO docker); fi
  fi
  "${DOCKER[@]}" info >/dev/null 2>&1 || fail 'el servicio de Docker no responde'
  local version
  version=$("${DOCKER[@]}" compose version --short 2>/dev/null | tr -d v) || fail 'falta el plugin Docker Compose v2'
  if [[ $(printf '%s\n%s\n' "$COMPOSE_MIN" "$version" | sort -V | head -1) != "$COMPOSE_MIN" ]]; then
    fail "Docker Compose $version es antiguo; se requiere $COMPOSE_MIN o posterior"
  fi
  echo "Docker y Compose $version listos."
}

compose() { "${DOCKER[@]}" compose "$@"; }
