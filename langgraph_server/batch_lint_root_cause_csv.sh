#!/usr/bin/env bash
set -euo pipefail

LANGGRAPH_URL="${LANGGRAPH_URL:-http://127.0.0.1:2024}"
LANGGRAPH_ASSISTANT="${LANGGRAPH_ASSISTANT:-lint}"
LANGGRAPH_RECURSION_LIMIT="${LANGGRAPH_RECURSION_LIMIT:-1000}"
LINT_AGENT_BATCH_TIMEOUT="${LINT_AGENT_BATCH_TIMEOUT:-1200}"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required."
}

json_escape() {
  local text="$1"
  text=${text//\\/\\\\}
  text=${text//\"/\\\"}
  text=${text//$'\r'/\\r}
  text=${text//$'\n'/\\n}
  text=${text//$'\t'/\\t}
  printf '%s' "$text"
}

new_uuid() {
  if command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr '[:upper:]' '[:lower:]'
    return
  fi
  if [[ -r /proc/sys/kernel/random/uuid ]]; then
    tr '[:upper:]' '[:lower:]' </proc/sys/kernel/random/uuid
    return
  fi
  die "uuidgen or /proc/sys/kernel/random/uuid is required to create thread IDs."
}

default_user_id() {
  if [[ -n "${USERNAME:-}" ]]; then
    printf 'cli:%s' "$USERNAME"
  elif [[ -n "${USER:-}" ]]; then
    printf 'cli:%s' "$USER"
  else
    printf 'cli:anonymous'
  fi
}

post_json() {
  local path="$1"
  local body="$2"
  local timeout="$3"
  local url="${LANGGRAPH_URL%/}$path"

  printf '%s' "$body" | curl -fsS \
    --max-time "$timeout" \
    -H 'Accept: application/json' \
    -H 'Content-Type: application/json; charset=utf-8' \
    --data-binary @- \
    "$url"
}

extract_ai_text() {
  local response_file="$1"
  command -v jq >/dev/null 2>&1 || return 1
  jq -r '
    def msg_text:
      if (.content | type) == "string" then
        .content
      elif (.content | type) == "array" then
        [ .content[]? | .text? // .content? // empty ] | join("")
      else
        ""
      end;

    (.messages // .values.messages // [])
    | reverse
    | map(
        select(((.type // .role // "") | ascii_downcase) as $role | $role == "ai" or $role == "assistant")
        | msg_text
      )
    | .[0] // empty
  ' "$response_file"
}

build_metadata_json() {
  local user_id="$1"
  local authenticated="$2"
  printf '{"source":"lint-agent-batch","assistant":"%s","user_id":"%s","authenticated":%s}' \
    "$(json_escape "$LANGGRAPH_ASSISTANT")" \
    "$(json_escape "$user_id")" \
    "$authenticated"
}

build_context_json() {
  local thread_id="$1"
  local user_id="$2"
  local authenticated="$3"
  printf '{"user_id":"%s","thread_id":"%s","authenticated":%s}' \
    "$(json_escape "$user_id")" \
    "$(json_escape "$thread_id")" \
    "$authenticated"
}

build_thread_json() {
  local thread_id="$1"
  local user_id="$2"
  local authenticated="$3"
  local metadata

  metadata="$(build_metadata_json "$user_id" "$authenticated")"
  printf '{"thread_id":"%s","metadata":%s,"if_exists":"do_nothing","graph_id":"%s"}' \
    "$(json_escape "$thread_id")" \
    "$metadata" \
    "$(json_escape "$LANGGRAPH_ASSISTANT")"
}

build_run_json() {
  local thread_id="$1"
  local user_id="$2"
  local authenticated="$3"
  local prompt="$4"
  local metadata context

  metadata="$(build_metadata_json "$user_id" "$authenticated")"
  context="$(build_context_json "$thread_id" "$user_id" "$authenticated")"
  printf '{"assistant_id":"%s","input":{"messages":[{"role":"user","content":"%s"}]},"metadata":%s,"context":%s,"if_not_exists":"create","config":{"recursion_limit":%s}}' \
    "$(json_escape "$LANGGRAPH_ASSISTANT")" \
    "$(json_escape "$prompt")" \
    "$metadata" \
    "$context" \
    "$LANGGRAPH_RECURSION_LIMIT"
}

if [[ -z "${BASH_VERSION:-}" ]]; then
  die "this script must be run with bash."
fi

require_command curl
require_command find
require_command sort
require_command date
require_command basename

[[ "$LANGGRAPH_RECURSION_LIMIT" =~ ^[0-9]+$ ]] || die "LANGGRAPH_RECURSION_LIMIT must be an integer."
[[ "$LINT_AGENT_BATCH_TIMEOUT" =~ ^[0-9]+$ ]] || die "LINT_AGENT_BATCH_TIMEOUT must be an integer number of seconds."

printf '请输入 tar 和 lint 报告所在文件夹绝对路径: '
IFS= read -r input_dir || die "failed to read input directory."

if [[ "$input_dir" == "~" ]]; then
  input_dir="$HOME"
elif [[ "$input_dir" == \~/* ]]; then
  input_dir="${input_dir/#\~/$HOME}"
fi
[[ "$input_dir" = /* ]] || die "please enter an absolute directory path."
[[ -d "$input_dir" ]] || die "directory does not exist: $input_dir"
input_dir="$(cd "$input_dir" && pwd -P)"

csv_paths=()
tar_paths=()
stems=()
missing_tars=()
missing_csvs=()

while IFS= read -r -d '' csv_path; do
  csv_name="$(basename "$csv_path")"
  [[ "$csv_name" == *_root_cause*.csv ]] && continue

  stem="${csv_name%.csv}"
  tar_path="$input_dir/$stem.tar.xz"
  if [[ -f "$tar_path" ]]; then
    csv_paths+=("$csv_path")
    tar_paths+=("$tar_path")
    stems+=("$stem")
  else
    missing_tars+=("$csv_name")
  fi
done < <(find "$input_dir" -maxdepth 1 -type f -name '*.csv' -print0 | sort -z)

while IFS= read -r -d '' tar_path; do
  tar_name="$(basename "$tar_path")"
  stem="${tar_name%.tar.xz}"
  [[ -f "$input_dir/$stem.csv" ]] || missing_csvs+=("$tar_name")
done < <(find "$input_dir" -maxdepth 1 -type f -name '*.tar.xz' -print0 | sort -z)

for item in "${missing_tars[@]}"; do
  warn "skip $item: matching ${item%.csv}.tar.xz not found."
done
for item in "${missing_csvs[@]}"; do
  warn "skip $item: matching ${item%.tar.xz}.csv not found."
done

task_count="${#csv_paths[@]}"
[[ "$task_count" -gt 0 ]] || die "no matched .csv + .tar.xz pairs found in $input_dir."

printf '找到 %s 组同名 lint 报告和 Verilog 源码包。\n' "$task_count"
printf 'Agent Server: %s\n' "$LANGGRAPH_URL"
curl -fsS --max-time 3 "${LANGGRAPH_URL%/}/ok" >/dev/null || die "Agent Server is not reachable at $LANGGRAPH_URL."

if [[ -n "${LANGGRAPH_USER_ID:-}" ]]; then
  user_id="$LANGGRAPH_USER_ID"
  authenticated=true
else
  user_id="$(default_user_id)"
  authenticated=false
fi

run_stamp="$(date +%Y%m%d_%H%M%S)"
log_dir="$input_dir/.lint_agent_batch_$run_stamp"
mkdir -p "$log_dir"

success_count=0
failure_count=0

for index in "${!csv_paths[@]}"; do
  csv_path="${csv_paths[$index]}"
  tar_path="${tar_paths[$index]}"
  stem="${stems[$index]}"
  output_csv="$input_dir/${stem}_root_cause_${run_stamp}.csv"
  thread_id="$(new_uuid)"
  response_file="$log_dir/${stem}.${thread_id}.response.json"

  prompt="${csv_path} 为 lint 报告路径，${tar_path} 为 Verilog 源代码包路径，请分析这些 lint 告警的根因，生成根因分析 CSV。请将最终 CSV 写入 ${output_csv}，并在完成二次复核和验证后再结束。"

  printf '\n[%s/%s] %s\n' "$((index + 1))" "$task_count" "$stem"
  printf 'thread_id: %s\n' "$thread_id"
  printf 'lint: %s\n' "$csv_path"
  printf 'source: %s\n' "$tar_path"
  printf 'output: %s\n' "$output_csv"

  thread_body="$(build_thread_json "$thread_id" "$user_id" "$authenticated")"
  if ! post_json "/threads" "$thread_body" 30 >/dev/null; then
    warn "failed to pre-create thread metadata for $stem; will still submit the run."
  fi

  run_body="$(build_run_json "$thread_id" "$user_id" "$authenticated" "$prompt")"
  if ! post_json "/threads/$thread_id/runs/wait" "$run_body" "$LINT_AGENT_BATCH_TIMEOUT" >"$response_file"; then
    warn "agent run failed for $stem. Response log: $response_file"
    failure_count=$((failure_count + 1))
    continue
  fi

  if grep -q '"__interrupt__"' "$response_file"; then
    warn "agent returned a tool-approval interrupt for $stem. Disable AGENT_TOOL_APPROVAL_ENABLED for batch mode or use the Python CLI with --auto-approve."
    warn "response log: $response_file"
    failure_count=$((failure_count + 1))
    continue
  fi

  assistant_text=""
  if assistant_text="$(extract_ai_text "$response_file" 2>/dev/null)" && [[ -n "$assistant_text" ]]; then
    printf 'assistant:\n%s\n' "$assistant_text"
  else
    printf 'assistant response saved: %s\n' "$response_file"
    if ! command -v jq >/dev/null 2>&1; then
      warn "jq is not installed; raw JSON response was saved instead of printing assistant text."
    fi
  fi

  if [[ -f "$output_csv" ]]; then
    printf 'done: %s\n' "$output_csv"
    success_count=$((success_count + 1))
  else
    warn "expected output CSV was not found after the run: $output_csv"
    warn "check assistant response and log: $response_file"
    failure_count=$((failure_count + 1))
  fi
done

printf '\n批处理完成：成功 %s，失败 %s。\n' "$success_count" "$failure_count"
printf '响应日志目录：%s\n' "$log_dir"

if [[ "$failure_count" -gt 0 ]]; then
  exit 1
fi
