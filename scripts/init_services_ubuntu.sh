#!/usr/bin/env bash
set -Eeuo pipefail

PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
SUPER_USER="${SUPER_USER:-postgres}"
SUPER_PASSWORD="${SUPER_PASSWORD:-123456}"
APP_USER="${APP_USER:-lint_agent}"
APP_PASSWORD="${APP_PASSWORD:-123456}"
LANGGRAPH_DB="${LANGGRAPH_DB:-langgraph_db}"
CHAINLIT_DB="${CHAINLIT_DB:-chainlit_db}"
APP_DIR=""
ENV_FILE=""
CHAINLIT_DATALAYER_DIR=""
CHAINLIT_DATALAYER_GIT_URL="${CHAINLIT_DATALAYER_GIT_URL:-https://github.com/Chainlit/chainlit-datalayer.git}"
CHAINLIT_DATALAYER_BRANCH="${CHAINLIT_DATALAYER_BRANCH:-main}"
MINIO_HOST="${MINIO_HOST:-127.0.0.1}"
MINIO_API_PORT="${MINIO_API_PORT:-9000}"
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-9001}"
MINIO_BUCKET="${MINIO_BUCKET:-chainlit-files}"
MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin123}"
MINIO_START_TIMEOUT="${MINIO_START_TIMEOUT:-15}"
SKIP_MINIO_SETUP=0
SKIP_CHAINLIT_MIGRATION=0
SKIP_PGVECTOR_CHECK=0

usage() {
  cat <<'USAGE'
Usage: init_services_ubuntu.sh [options]

Initialize the local ALINT-PRO services on Ubuntu. This script assumes psql is
available and MinIO/mc are already installed under <app-dir>/.local/minio/bin.

Options:
  --pg-host VALUE                  PostgreSQL host (default: 127.0.0.1)
  --pg-port VALUE                  PostgreSQL port (default: 5432)
  --super-user VALUE               PostgreSQL superuser (default: postgres)
  --super-password VALUE           PostgreSQL superuser password (default: 123456)
  --app-user VALUE                 Application DB role (default: lint_agent)
  --app-password VALUE             Application DB role password (default: 123456)
  --langgraph-db VALUE             LangGraph database (default: langgraph_db)
  --chainlit-db VALUE              Chainlit database (default: chainlit_db)
  --app-dir VALUE                  lint_agent directory (default: script parent)
  --env-file VALUE                 .env path (default: <app-dir>/.env)
  --chainlit-datalayer-dir VALUE   chainlit-datalayer directory
  --chainlit-datalayer-git-url VALUE
                                  chainlit-datalayer Git URL
  --chainlit-datalayer-branch VALUE
                                  chainlit-datalayer Git branch (default: main)
  --minio-host VALUE               MinIO host (default: 127.0.0.1)
  --minio-api-port VALUE           MinIO API port (default: 9000)
  --minio-console-port VALUE       MinIO console port (default: 9001)
  --minio-bucket VALUE             MinIO bucket (default: chainlit-files)
  --minio-root-user VALUE          MinIO root user (default: minioadmin)
  --minio-root-password VALUE      MinIO root password (default: minioadmin123)
  --minio-start-timeout VALUE      MinIO startup timeout seconds (default: 15)
  --skip-minio-setup               Do not start MinIO or create bucket
  --skip-chainlit-migration        Do not run Prisma migrations
  --skip-pgvector-check            Do not require pgvector/vector extension
  -h, --help                       Show this help
USAGE
}

log() {
  printf '[INFO] %s\n' "$*"
}

die() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pg-host) PG_HOST="${2:?}"; shift 2 ;;
    --pg-port) PG_PORT="${2:?}"; shift 2 ;;
    --super-user) SUPER_USER="${2:?}"; shift 2 ;;
    --super-password) SUPER_PASSWORD="${2:?}"; shift 2 ;;
    --app-user) APP_USER="${2:?}"; shift 2 ;;
    --app-password) APP_PASSWORD="${2:?}"; shift 2 ;;
    --langgraph-db) LANGGRAPH_DB="${2:?}"; shift 2 ;;
    --chainlit-db) CHAINLIT_DB="${2:?}"; shift 2 ;;
    --app-dir) APP_DIR="${2:?}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?}"; shift 2 ;;
    --chainlit-datalayer-dir) CHAINLIT_DATALAYER_DIR="${2:?}"; shift 2 ;;
    --chainlit-datalayer-git-url) CHAINLIT_DATALAYER_GIT_URL="${2:?}"; shift 2 ;;
    --chainlit-datalayer-branch) CHAINLIT_DATALAYER_BRANCH="${2:?}"; shift 2 ;;
    --minio-host) MINIO_HOST="${2:?}"; shift 2 ;;
    --minio-api-port) MINIO_API_PORT="${2:?}"; shift 2 ;;
    --minio-console-port) MINIO_CONSOLE_PORT="${2:?}"; shift 2 ;;
    --minio-bucket) MINIO_BUCKET="${2:?}"; shift 2 ;;
    --minio-root-user) MINIO_ROOT_USER="${2:?}"; shift 2 ;;
    --minio-root-password) MINIO_ROOT_PASSWORD="${2:?}"; shift 2 ;;
    --minio-start-timeout) MINIO_START_TIMEOUT="${2:?}"; shift 2 ;;
    --skip-minio-setup) SKIP_MINIO_SETUP=1; shift ;;
    --skip-chainlit-migration) SKIP_CHAINLIT_MIGRATION=1; shift ;;
    --skip-pgvector-check) SKIP_PGVECTOR_CHECK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$APP_DIR" ]]; then
  APP_DIR="$(cd -- "$script_dir/.." && pwd)"
else
  APP_DIR="$(realpath -m "$APP_DIR")"
fi

if [[ -z "$ENV_FILE" ]]; then
  ENV_FILE="$APP_DIR/.env"
else
  ENV_FILE="$(realpath -m "$ENV_FILE")"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  die ".env not found at '$ENV_FILE'. Create it from .env.example and edit it before running this script."
fi

quote_pg_identifier() {
  printf '%s' "$1" | sed 's/"/""/g; s/^/"/; s/$/"/'
}

quote_pg_literal() {
  printf '%s' "$1" | sed "s/'/''/g; s/^/'/; s/$/'/"
}

urlencode() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

postgres_url() {
  local database="$1"
  local user password
  user="$(urlencode "$APP_USER")"
  password="$(urlencode "$APP_PASSWORD")"
  printf 'postgresql://%s:%s@%s:%s/%s' "$user" "$password" "$PG_HOST" "$PG_PORT" "$database"
}

PSQL_EXE="psql"
MINIO_BIN_DIR="$APP_DIR/.local/minio/bin"
MINIO_DATA_DIR="$APP_DIR/.local/minio/data"
MINIO_EXE="$MINIO_BIN_DIR/minio"
MC_EXE="$MINIO_BIN_DIR/mc"

REPO_ROOT="$(cd -- "$APP_DIR/.." && pwd)"

psql_exec() {
  local database="$1"
  shift
  PGPASSWORD="$SUPER_PASSWORD" "$PSQL_EXE" \
    -h "$PG_HOST" \
    -p "$PG_PORT" \
    -U "$SUPER_USER" \
    -d "$database" \
    -v ON_ERROR_STOP=1 \
    -X \
    "$@"
}

psql_scalar() {
  local database="$1"
  local sql="$2"
  psql_exec "$database" -t -A -c "$sql"
}

ensure_role() {
  local role_ident user_lit pass_lit
  role_ident="$(quote_pg_identifier "$APP_USER")"
  user_lit="$(quote_pg_literal "$APP_USER")"
  pass_lit="$(quote_pg_literal "$APP_PASSWORD")"

  log "Creating/updating PostgreSQL role: $APP_USER"
  psql_exec postgres <<SQL >/dev/null
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $user_lit) THEN
    CREATE ROLE $role_ident LOGIN PASSWORD $pass_lit;
  ELSE
    ALTER ROLE $role_ident WITH LOGIN PASSWORD $pass_lit;
  END IF;
END
\$\$;
SQL
}

ensure_app_user_not_superuser() {
  if [[ "$APP_USER" == "$SUPER_USER" ]]; then
    die "APP_USER must be a dedicated PostgreSQL role, not the PostgreSQL administrator. Use the role configured in .env, for example --app-user lint_agent --app-password 123456."
  fi
}

ensure_database() {
  local database="$1"
  local db_ident db_lit role_ident exists
  db_ident="$(quote_pg_identifier "$database")"
  db_lit="$(quote_pg_literal "$database")"
  role_ident="$(quote_pg_identifier "$APP_USER")"

  exists="$(psql_scalar postgres "SELECT 1 FROM pg_database WHERE datname = $db_lit;" | tr -d '[:space:]')"
  if [[ "$exists" == "1" ]]; then
    log "Database exists: $database"
    psql_exec postgres -c "ALTER DATABASE $db_ident OWNER TO $role_ident;" >/dev/null
  else
    log "Creating database: $database"
    psql_exec postgres -c "CREATE DATABASE $db_ident OWNER $role_ident;" >/dev/null
  fi

  psql_exec "$database" -c "GRANT ALL ON SCHEMA public TO $role_ident; ALTER SCHEMA public OWNER TO $role_ident;" >/dev/null
}

ensure_extension() {
  local database="$1"
  local extension="$2"
  local extension_ident
  extension_ident="$(quote_pg_identifier "$extension")"
  log "Ensuring extension in $database: $extension"
  if ! output="$(psql_exec "$database" -c "CREATE EXTENSION IF NOT EXISTS $extension_ident;" 2>&1)"; then
    printf '%s\n' "$output" >&2
    die "Cannot create extension '$extension' in '$database'."
  fi
}

is_chainlit_datalayer_dir() {
  local path="$1"
  [[ -f "$path/package.json" && -f "$path/prisma/schema.prisma" ]]
}

ensure_chainlit_datalayer_dir() {
  local candidates=()
  local candidate target

  if [[ -n "$CHAINLIT_DATALAYER_DIR" ]]; then
    candidates+=("$CHAINLIT_DATALAYER_DIR")
  fi
  candidates+=("$REPO_ROOT/chainlit-datalayer")
  candidates+=("$APP_DIR/chainlit-datalayer")

  for candidate in "${candidates[@]}"; do
    candidate="$(realpath -m "$candidate")"
    if is_chainlit_datalayer_dir "$candidate"; then
      CHAINLIT_DATALAYER_DIR="$candidate"
      return
    fi
  done

  target="$REPO_ROOT/chainlit-datalayer"
  command -v git >/dev/null || die "chainlit-datalayer not found and git is unavailable. Clone it to '$target' or pass --chainlit-datalayer-dir."

  log "chainlit-datalayer not found. Cloning to: $target"
  git clone --depth 1 --branch "$CHAINLIT_DATALAYER_BRANCH" "$CHAINLIT_DATALAYER_GIT_URL" "$target" || die "git clone failed for chainlit-datalayer."
  CHAINLIT_DATALAYER_DIR="$(realpath -m "$target")"
}

run_chainlit_migration() {
  ensure_chainlit_datalayer_dir
  command -v npm >/dev/null || die "npm is required for Chainlit Prisma migration."
  command -v npx >/dev/null || die "npx is required for Chainlit Prisma migration."

  log "Running Chainlit Prisma migration in: $CHAINLIT_DATALAYER_DIR"
  (
    cd "$CHAINLIT_DATALAYER_DIR"
    if [[ ! -d node_modules ]]; then
      if [[ -f package-lock.json ]]; then
        npm ci
      else
        npm install
      fi
    fi
    DATABASE_URL="$(postgres_url "$CHAINLIT_DB")" npx prisma migrate deploy --schema "$CHAINLIT_DATALAYER_DIR/prisma/schema.prisma"
  )
}

tcp_port_open() {
  local host="$1"
  local port="$2"
  timeout 1 bash -c ":</dev/tcp/$host/$port" >/dev/null 2>&1
}

wait_minio_ready() {
  local health_url="http://${MINIO_HOST}:${MINIO_API_PORT}/minio/health/live"
  local deadline=$((SECONDS + MINIO_START_TIMEOUT))
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 2 "$health_url" >/dev/null 2>&1; then
      log "MinIO is ready: http://${MINIO_HOST}:${MINIO_API_PORT}"
      return
    fi
    sleep 1
  done
  die "MinIO did not become ready within ${MINIO_START_TIMEOUT}s. Check port ${MINIO_API_PORT} and $APP_DIR/.local/minio/minio.log."
}

start_local_minio() {
  if tcp_port_open "$MINIO_HOST" "$MINIO_API_PORT"; then
    log "MinIO API port is already open: ${MINIO_HOST}:${MINIO_API_PORT}"
    return
  fi

  mkdir -p "$MINIO_DATA_DIR" "$APP_DIR/.local/minio"

  log "Starting local MinIO from: $MINIO_EXE"
  MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD" \
    nohup "$MINIO_EXE" server "$MINIO_DATA_DIR" \
      --address ":${MINIO_API_PORT}" \
      --console-address ":${MINIO_CONSOLE_PORT}" \
      >"$APP_DIR/.local/minio/minio.log" 2>&1 &

  log "MinIO process started: pid=$!"
  wait_minio_ready
}

mc_exec() {
  "$MC_EXE" "$@"
}

ensure_minio_bucket() {
  local alias_name="mcp-alint-local"
  local endpoint="http://${MINIO_HOST}:${MINIO_API_PORT}"
  log "Configuring MinIO bucket: $MINIO_BUCKET"
  mc_exec alias set "$alias_name" "$endpoint" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  mc_exec mb --ignore-existing "${alias_name}/${MINIO_BUCKET}" >/dev/null
  mc_exec alias remove "$alias_name" >/dev/null 2>&1 || true
}

ensure_minio() {
  if [[ "$SKIP_MINIO_SETUP" == "1" ]]; then
    log "Skipping MinIO setup."
    return
  fi
  start_local_minio
  ensure_minio_bucket
}

log "Project directory: $APP_DIR"
log "Env file: $ENV_FILE"
log "Using psql: $PSQL_EXE"
if [[ "$SKIP_MINIO_SETUP" != "1" ]]; then
  log "MinIO binary directory: $MINIO_BIN_DIR"
  log "MinIO data directory: $MINIO_DATA_DIR"
fi

ensure_app_user_not_superuser
ensure_role
ensure_database "$LANGGRAPH_DB"
ensure_database "$CHAINLIT_DB"

ensure_extension "$CHAINLIT_DB" "pgcrypto"

if [[ "$SKIP_PGVECTOR_CHECK" != "1" ]]; then
  ensure_extension "$LANGGRAPH_DB" "vector"
fi

if [[ "$SKIP_CHAINLIT_MIGRATION" != "1" ]]; then
  run_chainlit_migration
else
  log "Skipping Chainlit Prisma migration."
fi

ensure_minio

log "Connectivity checks"
psql_exec "$LANGGRAPH_DB" -c "SELECT current_database(), current_user;"
psql_exec "$CHAINLIT_DB" -c "SELECT current_database(), current_user;"
if [[ "$SKIP_MINIO_SETUP" != "1" ]]; then
  mc_exec alias set mcp-alint-check "http://${MINIO_HOST}:${MINIO_API_PORT}" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  mc_exec ls "mcp-alint-check/${MINIO_BUCKET}"
  mc_exec alias remove mcp-alint-check >/dev/null 2>&1 || true
fi

printf '\n[DONE] Local services initialized.\n'
printf '       LangGraph DB: %s\n' "$LANGGRAPH_DB"
printf '       Chainlit DB:  %s\n' "$CHAINLIT_DB"
printf '       App user:     %s\n' "$APP_USER"
if [[ "$SKIP_MINIO_SETUP" != "1" ]]; then
  printf '       MinIO API:    http://%s:%s\n' "$MINIO_HOST" "$MINIO_API_PORT"
  printf '       MinIO bucket: %s\n' "$MINIO_BUCKET"
fi
