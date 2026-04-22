#!/usr/bin/env bash
set -euo pipefail

PG_USER="${PG_USER:-admin}"
PG_PASSWORD="${PG_PASSWORD:-123456}"
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
LANGGRAPH_DB="${LANGGRAPH_DB:-langgraph_db}"
CHAINLIT_DB="${CHAINLIT_DB:-chainlit_db}"
CHAINLIT_DATALAYER_GIT_URL="${CHAINLIT_DATALAYER_GIT_URL:-https://github.com/Chainlit/chainlit-datalayer.git}"
CHAINLIT_DATALAYER_BRANCH="${CHAINLIT_DATALAYER_BRANCH:-main}"

# ── MinIO ─────────────────────────────────────────────────────────────────
MINIO_ROOT_USER="${MINIO_ROOT_USER:-admin}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-admin123456}"
MINIO_HOST="${MINIO_HOST:-127.0.0.1}"
MINIO_API_PORT="${MINIO_API_PORT:-9000}"
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-9001}"
MINIO_BUCKET="${MINIO_BUCKET:-chainlit-files}"
MINIO_DATA_DIR="${MINIO_DATA_DIR:-/data/minio}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ROOT_DIR="$(cd -- "${APP_DIR}/.." && pwd)"
ENV_FILE="${APP_DIR}/.env"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[ERROR] Missing command: $1"
    exit 1
  fi
}

sql_escape_literal() {
  printf "%s" "$1" | sed "s/'/''/g"
}

update_or_append_env() {
  local key="$1"
  local value="$2"
  local escaped
  escaped="$(printf "%s" "$value" | sed 's/[&]/\\&/g')"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${escaped}|" "$ENV_FILE"
  else
    printf "%s=%s\n" "$key" "$value" >> "$ENV_FILE"
  fi
}

find_chainlit_datalayer_dir() {
  if [[ -d "${ROOT_DIR}/chainlit-datalayer" ]]; then
    printf "%s" "${ROOT_DIR}/chainlit-datalayer"
    return 0
  fi
  if [[ -d "${APP_DIR}/chainlit-datalayer" ]]; then
    printf "%s" "${APP_DIR}/chainlit-datalayer"
    return 0
  fi
  return 1
}

ensure_chainlit_datalayer_dir() {
  local target_dir="${ROOT_DIR}/chainlit-datalayer"
  local found_dir
  if found_dir="$(find_chainlit_datalayer_dir)"; then
    printf "%s" "$found_dir"
    return 0
  fi

  require_cmd git
  echo "[INFO] chainlit-datalayer not found, cloning from ${CHAINLIT_DATALAYER_GIT_URL}" >&2
  git clone --depth 1 --branch "${CHAINLIT_DATALAYER_BRANCH}" "${CHAINLIT_DATALAYER_GIT_URL}" "${target_dir}" >&2
  printf "%s" "${target_dir}"
}

run_psql_as_postgres() {
  sudo -u postgres psql -v ON_ERROR_STOP=1 "$@"
}

main() {
  require_cmd sudo
  require_cmd grep
  require_cmd sed

  # ── 安装 PostgreSQL（如果尚未安装）─────────────────────────────────────
  if ! command -v psql >/dev/null 2>&1; then
    echo "[INFO] PostgreSQL not found, installing via apt..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq postgresql-common ca-certificates >/dev/null
    sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
    sudo apt-get update -qq
    sudo apt-get install -y -qq postgresql >/dev/null
    echo "[INFO] PostgreSQL installed"
  else
    echo "[INFO] PostgreSQL already installed: $(psql --version)"
  fi

  echo "[INFO] Starting PostgreSQL service..."
  sudo systemctl enable --now postgresql >/dev/null 2>&1 || true

  if ! sudo -u postgres psql -tAc "SELECT 1" >/dev/null 2>&1; then
    echo "[ERROR] Cannot connect as postgres user. Check service and local auth."
    exit 1
  fi

  local user_lit pass_lit
  user_lit="$(sql_escape_literal "$PG_USER")"
  pass_lit="$(sql_escape_literal "$PG_PASSWORD")"

  echo "[INFO] Creating/updating role: ${PG_USER}"
  run_psql_as_postgres -d postgres -c "DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${user_lit}') THEN
    CREATE ROLE \"${PG_USER}\" LOGIN PASSWORD '${pass_lit}';
  ELSE
    ALTER ROLE \"${PG_USER}\" WITH LOGIN PASSWORD '${pass_lit}';
  END IF;
END
\$\$;"

  for db_name in "$LANGGRAPH_DB" "$CHAINLIT_DB"; do
    local db_lit exists
    db_lit="$(sql_escape_literal "$db_name")"
    exists="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${db_lit}'" | tr -d '[:space:]')"
    if [[ "$exists" != "1" ]]; then
      echo "[INFO] Creating database: ${db_name}"
      run_psql_as_postgres -d postgres -c "CREATE DATABASE \"${db_name}\" OWNER \"${PG_USER}\";"
    else
      echo "[INFO] Database exists: ${db_name}"
      run_psql_as_postgres -d postgres -c "ALTER DATABASE \"${db_name}\" OWNER TO \"${PG_USER}\";"
    fi
  done

  if [[ -f "$ENV_FILE" ]]; then
    echo "[INFO] Updating env file: ${ENV_FILE}"
    update_or_append_env "CHECKPOINTER_BACKEND" "postgres"
    update_or_append_env "CHECKPOINTER_DB_URI" "postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/${LANGGRAPH_DB}"
    update_or_append_env "CHECKPOINTER_AUTO_SETUP" "true"
    update_or_append_env "DATABASE_URL" "postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/${CHAINLIT_DB}"
  else
    echo "[WARN] .env not found at ${ENV_FILE}; skipping env update."
  fi

  local datalayer_dir
  datalayer_dir="$(ensure_chainlit_datalayer_dir)"
  if command -v npx >/dev/null 2>&1; then
    echo "[INFO] Running chainlit-datalayer migration in: ${datalayer_dir}"
    (
      cd "$datalayer_dir"
      if [[ ! -d node_modules ]]; then
        echo "[INFO] node_modules not found, installing dependencies..."
        if [[ -f package-lock.json ]]; then
          npm ci
        else
          npm install
        fi
      fi
      DATABASE_URL="postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/${CHAINLIT_DB}" \
        npx prisma migrate deploy
    )
  else
    echo "[WARN] npx not found. Skip migration."
    echo "[WARN] Install Node.js and run manually:"
    echo "       cd ${datalayer_dir}"
    echo "       DATABASE_URL=postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/${CHAINLIT_DB} npx prisma migrate deploy"
  fi

  # ── MinIO 安装与配置 ────────────────────────────────────────────────────
  echo
  echo "[INFO] ── MinIO setup ──"

  # 1) 安装 minio 二进制（如果不存在）
  if ! command -v minio >/dev/null 2>&1; then
    echo "[INFO] MinIO binary not found, downloading..."
    local tmp_minio
    tmp_minio="$(mktemp)"
    wget -q -O "$tmp_minio" https://dl.min.io/server/minio/release/linux-amd64/minio
    chmod +x "$tmp_minio"
    sudo mv "$tmp_minio" /usr/local/bin/minio
    echo "[INFO] MinIO installed to /usr/local/bin/minio"
  else
    echo "[INFO] MinIO binary already installed: $(which minio)"
  fi

  # 2) 安装 mc 客户端（如果不存在）
  if ! command -v mc >/dev/null 2>&1; then
    echo "[INFO] MinIO client (mc) not found, downloading..."
    local tmp_mc
    tmp_mc="$(mktemp)"
    wget -q -O "$tmp_mc" https://dl.min.io/client/mc/release/linux-amd64/mc
    chmod +x "$tmp_mc"
    sudo mv "$tmp_mc" /usr/local/bin/mc
    echo "[INFO] mc installed to /usr/local/bin/mc"
  else
    echo "[INFO] mc already installed: $(which mc)"
  fi

  # 3) 创建数据目录和 systemd 用户
  if ! id minio-user >/dev/null 2>&1; then
    echo "[INFO] Creating minio-user system account"
    sudo useradd -r -s /sbin/nologin minio-user
  fi
  sudo mkdir -p "$MINIO_DATA_DIR"
  sudo chown minio-user:minio-user "$MINIO_DATA_DIR"

  # 4) 写入环境配置
  sudo tee /etc/default/minio >/dev/null <<MINIO_ENV
MINIO_ROOT_USER=${MINIO_ROOT_USER}
MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}
MINIO_VOLUMES=${MINIO_DATA_DIR}
MINIO_OPTS="--address :${MINIO_API_PORT} --console-address :${MINIO_CONSOLE_PORT}"
MINIO_ENV

  # 5) 创建 systemd 服务
  sudo tee /etc/systemd/system/minio.service >/dev/null <<'MINIO_UNIT'
[Unit]
Description=MinIO Object Storage
After=network-online.target
Wants=network-online.target

[Service]
User=minio-user
Group=minio-user
EnvironmentFile=/etc/default/minio
ExecStart=/usr/local/bin/minio server $MINIO_VOLUMES $MINIO_OPTS
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
MINIO_UNIT

  # 6) 启动 MinIO
  sudo systemctl daemon-reload
  sudo systemctl enable --now minio
  echo "[INFO] Waiting for MinIO to start..."
  local retries=0
  while ! curl -sf "http://${MINIO_HOST}:${MINIO_API_PORT}/minio/health/live" >/dev/null 2>&1; do
    retries=$((retries + 1))
    if [[ $retries -ge 15 ]]; then
      echo "[ERROR] MinIO did not become healthy within 15s"
      echo "[ERROR] Check: sudo systemctl status minio / sudo journalctl -u minio"
      exit 1
    fi
    sleep 1
  done
  echo "[INFO] MinIO is running (API :${MINIO_API_PORT}, Console :${MINIO_CONSOLE_PORT})"

  # 7) 创建 bucket（幂等）
  mc alias set mlocal "http://${MINIO_HOST}:${MINIO_API_PORT}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" >/dev/null 2>&1
  if mc ls "mlocal/${MINIO_BUCKET}" >/dev/null 2>&1; then
    echo "[INFO] Bucket already exists: ${MINIO_BUCKET}"
  else
    mc mb "mlocal/${MINIO_BUCKET}" >/dev/null 2>&1
    echo "[INFO] Bucket created: ${MINIO_BUCKET}"
  fi
  mc alias rm mlocal >/dev/null 2>&1 || true

  # 8) 回写 MinIO 配置到 .env
  if [[ -f "$ENV_FILE" ]]; then
    update_or_append_env "BUCKET_NAME" "${MINIO_BUCKET}"
    update_or_append_env "APP_AWS_ACCESS_KEY" "${MINIO_ROOT_USER}"
    update_or_append_env "APP_AWS_SECRET_KEY" "${MINIO_ROOT_PASSWORD}"
    update_or_append_env "APP_AWS_REGION" "us-east-1"
    update_or_append_env "DEV_AWS_ENDPOINT" "http://${MINIO_HOST}:${MINIO_API_PORT}"
  fi

  # ── 最终连通性检查 ──────────────────────────────────────────────────────
  echo
  echo "[INFO] Connectivity checks..."
  PGPASSWORD="$PG_PASSWORD" psql "postgresql://${PG_USER}@${PG_HOST}:${PG_PORT}/${LANGGRAPH_DB}" -c "SELECT current_database(), current_user;"
  PGPASSWORD="$PG_PASSWORD" psql "postgresql://${PG_USER}@${PG_HOST}:${PG_PORT}/${CHAINLIT_DB}" -c "SELECT current_database(), current_user;"

  echo
  echo "[DONE] All services initialized."
  echo "       PostgreSQL: user=${PG_USER} | langgraph_db=${LANGGRAPH_DB} | chainlit_db=${CHAINLIT_DB}"
  echo "       MinIO:      API=http://${MINIO_HOST}:${MINIO_API_PORT} | Console=http://${MINIO_HOST}:${MINIO_CONSOLE_PORT} | Bucket=${MINIO_BUCKET}"
}

main "$@"
