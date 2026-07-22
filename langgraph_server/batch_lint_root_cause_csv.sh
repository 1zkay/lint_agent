#!/usr/bin/env bash
set -euo pipefail

LANGGRAPH_URL="${LANGGRAPH_URL:-http://127.0.0.1:2024}"
LANGGRAPH_ASSISTANT="${LANGGRAPH_ASSISTANT:-lint}"
LANGGRAPH_RECURSION_LIMIT="${LANGGRAPH_RECURSION_LIMIT:-1000}"
LINT_AGENT_BATCH_TIMEOUT="${LINT_AGENT_BATCH_TIMEOUT:-7200}"
LINT_AGENT_BATCH_JOBS="${LINT_AGENT_BATCH_JOBS:-1}"
AGENT_DISPLAY_VERSION="v12"

batch_jobs="$LINT_AGENT_BATCH_JOBS"
output_dir=""
SOURCE_ARCHIVE_SUFFIXES=(
  ".tar.xz"
  ".tar.gz"
  ".tar.bz2"
  ".tbz2"
  ".tgz"
  ".txz"
  ".tar"
  ".zip"
  ".7z"
  ".rar"
  ".cab"
)

show_usage() {
  printf '用法: %s [-j 并发数] [-out_dir 输出目录]\n' "${0##*/}"
  printf '智能体版本: %s\n' "$AGENT_DISPLAY_VERSION"
  printf '\n'
  printf '选项:\n'
  printf '  -j, --jobs N    同时运行的智能体分析数量，默认 1。\n'
  printf '  -out_dir DIR    指定根因分析 CSV 输出目录；未指定时只保留 reports 默认输出。\n'
  printf '                 同时支持 --out_dir 和 --out-dir。\n'
  printf '  -h, --help      显示帮助。\n'
  printf '\n'
  printf '源码输入:\n'
  printf '  同名源码目录，或同名 .tar/.tar.gz/.tgz/.tar.bz2/.tbz2/.tar.xz/.txz/.zip/.7z/.rar/.cab 源码包。\n'
  printf '输出:\n'
  printf '  reports/<项目名>_root_cause_<YYYYMMDD_HHMMSS>.csv，项目名来自同名 lint CSV 文件名。\n'
  printf '\n'
  printf '环境变量:\n'
  printf '  LANGGRAPH_URL              默认 http://127.0.0.1:2024\n'
  printf '  LANGGRAPH_ASSISTANT        默认 lint\n'
  printf '  LANGGRAPH_RECURSION_LIMIT  默认 1000\n'
  printf '  LINT_AGENT_BATCH_TIMEOUT   单个任务等待秒数，默认 7200\n'
  printf '  LINT_AGENT_BATCH_JOBS      默认并发数，默认 1\n'
  printf '  ALINT_HOST_POSIX_SOURCE_ROOT  Linux 宿主机挂载源路径，默认 /\n'
  printf '  ALINT_HOST_POSIX_MOUNT_ROOT   容器内挂载路径，默认 /host/root\n'
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -j|--jobs)
        [[ $# -ge 2 ]] || die "$1 requires a positive integer."
        batch_jobs="$2"
        shift 2
        ;;
      --jobs=*)
        batch_jobs="${1#*=}"
        shift
        ;;
      -out_dir|--out_dir|--out-dir)
        [[ $# -ge 2 ]] || die "$1 requires an absolute directory path."
        output_dir="$2"
        shift 2
        ;;
      -out_dir=*|--out_dir=*|--out-dir=*)
        output_dir="${1#*=}"
        shift
        ;;
      -h|--help)
        show_usage
        exit 0
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done
}

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

trim_trailing_slash() {
  local value="$1"

  while [[ "$value" != "/" && "$value" == */ ]]; do
    value="${value%/}"
  done
  printf '%s' "$value"
}

expand_home_path() {
  local path="$1"

  if [[ "$path" == "~" ]]; then
    printf '%s' "$HOME"
  elif [[ "$path" == \~/* ]]; then
    printf '%s' "${path/#\~/$HOME}"
  else
    printf '%s' "$path"
  fi
}

translate_linux_host_path_for_container() {
  local path="$1"
  local source_root="${ALINT_HOST_POSIX_SOURCE_ROOT:-/}"
  local mount_root="${ALINT_HOST_POSIX_MOUNT_ROOT:-/host/root}"
  local prefix
  local rel

  [[ "$path" == /* ]] || {
    printf '%s' "$path"
    return 0
  }
  [[ -n "$source_root" && -n "$mount_root" ]] || {
    printf '%s' "$path"
    return 0
  }

  source_root="$(trim_trailing_slash "$source_root")"
  mount_root="$(trim_trailing_slash "$mount_root")"
  [[ "$source_root" == /* && "$mount_root" == /* ]] || {
    printf '%s' "$path"
    return 0
  }
  [[ -d "$mount_root" ]] || {
    printf '%s' "$path"
    return 0
  }

  if [[ "$path" == "$mount_root" || "$path" == "$mount_root"/* ]]; then
    printf '%s' "$path"
    return 0
  fi

  if [[ "$source_root" == "/" ]]; then
    if [[ "$path" == "/" ]]; then
      printf '%s' "$mount_root"
    else
      printf '%s/%s' "$mount_root" "${path#/}"
    fi
    return 0
  fi

  if [[ "$path" == "$source_root" ]]; then
    printf '%s' "$mount_root"
    return 0
  fi

  prefix="$source_root/"
  if [[ "$path" == "$prefix"* ]]; then
    rel="${path#"$prefix"}"
    printf '%s/%s' "$mount_root" "$rel"
    return 0
  fi

  printf '%s' "$path"
}

translate_container_path_for_host() {
  local path="$1"
  local source_root="${ALINT_HOST_POSIX_SOURCE_ROOT:-/}"
  local mount_root="${ALINT_HOST_POSIX_MOUNT_ROOT:-/host/root}"
  local prefix
  local rel

  [[ "$path" == /* ]] || {
    printf '%s' "$path"
    return 0
  }
  [[ -n "$source_root" && -n "$mount_root" ]] || {
    printf '%s' "$path"
    return 0
  }

  source_root="$(trim_trailing_slash "$source_root")"
  mount_root="$(trim_trailing_slash "$mount_root")"
  [[ "$source_root" == /* && "$mount_root" == /* ]] || {
    printf '%s' "$path"
    return 0
  }

  if [[ "$path" == "$mount_root" ]]; then
    printf '%s' "$source_root"
    return 0
  fi

  prefix="$mount_root/"
  if [[ "$path" == "$prefix"* ]]; then
    rel="${path#"$prefix"}"
    if [[ "$source_root" == "/" ]]; then
      printf '/%s' "$rel"
    else
      printf '%s/%s' "$source_root" "$rel"
    fi
    return 0
  fi

  printf '%s' "$path"
}

decode_mountinfo_path() {
  local path="$1"

  path=${path//\\040/ }
  path=${path//\\011/$'\t'}
  path=${path//\\012/$'\n'}
  path=${path//\\134/\\}
  printf '%s' "$path"
}

mountinfo_host_path() {
  local path="$1"
  local line
  local mount_root
  local mount_point
  local decoded_root
  local decoded_point
  local best_root=""
  local best_point=""
  local rel

  [[ "$path" == /* && -r /proc/self/mountinfo ]] || {
    printf '%s' "$path"
    return 0
  }

  while IFS= read -r line; do
    read -r _ _ _ mount_root mount_point _ <<<"$line"
    decoded_root="$(decode_mountinfo_path "$mount_root")"
    decoded_point="$(decode_mountinfo_path "$mount_point")"
    if [[ "$path" == "$decoded_point" || "$path" == "$decoded_point"/* ]]; then
      if [[ "${#decoded_point}" -gt "${#best_point}" ]]; then
        best_root="$decoded_root"
        best_point="$decoded_point"
      fi
    fi
  done </proc/self/mountinfo

  if [[ -z "$best_point" ]]; then
    printf '%s' "$path"
    return 0
  fi

  if [[ "$path" == "$best_point" ]]; then
    printf '%s' "$best_root"
    return 0
  fi

  rel="${path#"$best_point"/}"
  if [[ "$best_root" == "/" ]]; then
    printf '/%s' "$rel"
  else
    printf '%s/%s' "$best_root" "$rel"
  fi
}

display_path() {
  local path="$1"
  local mapped

  case "$path" in
    reports/*)
      path="$(pwd -P)/$path"
      ;;
    ./reports/*)
      path="$(pwd -P)/${path#./}"
      ;;
  esac

  mapped="$(translate_container_path_for_host "$path")"
  if [[ "$mapped" != "$path" ]]; then
    printf '%s' "$mapped"
    return 0
  fi

  mountinfo_host_path "$path"
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

post_json_to_file() {
  local path="$1"
  local body="$2"
  local timeout="$3"
  local output_file="$4"
  local url="${LANGGRAPH_URL%/}$path"

  printf '%s' "$body" | curl -fsS \
    --max-time "$timeout" \
    -H 'Accept: application/json' \
    -H 'Content-Type: application/json; charset=utf-8' \
    --data-binary @- \
    "$url" >"$output_file"
}

archive_stem() {
  local name="$1"
  local suffix

  for suffix in "${SOURCE_ARCHIVE_SUFFIXES[@]}"; do
    if [[ "$name" == *"$suffix" ]]; then
      printf '%s' "${name%"$suffix"}"
      return 0
    fi
  done
  return 1
}

collect_source_candidates() {
  local stem="$1"
  local suffix
  local candidate

  source_candidates=()
  for suffix in "${SOURCE_ARCHIVE_SUFFIXES[@]}"; do
    candidate="$input_dir/$stem$suffix"
    [[ -f "$candidate" ]] && source_candidates+=("$candidate")
  done

  candidate="$input_dir/$stem"
  [[ -d "$candidate" ]] && source_candidates+=("$candidate")
  return 0
}

format_candidates() {
  local candidate
  local separator=""

  for candidate in "$@"; do
    printf '%s%s' "$separator" "$(display_path "$candidate")"
    separator=", "
  done
  return 0
}

parse_report_path_from_response() {
  local response_file="$1"
  local input_message_id="$2"

  "$response_parser_python" -m langgraph_server.response_parsing \
    batch-response "$response_file" \
    --after-message-id "$input_message_id"
}

print_report_path() {
  local path="$1"
  local report_path

  report_path="$path"
  if [[ -n "$output_dir" ]]; then
    report_path="$output_dir/$(basename "$path")"
  fi
  printf 'report: %s\n' "$(display_path "$report_path")"
}

resolve_report_copy_source() {
  local path="$1"
  local candidate

  case "$path" in
    reports/*)
      candidate="$(pwd -P)/$path"
      ;;
    ./reports/*)
      candidate="$(pwd -P)/${path#./}"
      ;;
    /*)
      candidate="$path"
      ;;
    *)
      candidate="$path"
      ;;
  esac
  [[ -f "$candidate" ]] && {
    printf '%s' "$candidate"
    return 0
  }

  candidate="$(translate_linux_host_path_for_container "$path")"
  [[ -f "$candidate" ]] && {
    printf '%s' "$candidate"
    return 0
  }

  candidate="$(pwd -P)/reports/$(basename "$path")"
  [[ -f "$candidate" ]] && {
    printf '%s' "$candidate"
    return 0
  }

  return 1
}

copy_report_path_to_output_dir() {
  local path="$1"
  local source
  local source_abs
  local target

  [[ -n "$output_dir" ]] || return 0

  if ! source="$(resolve_report_copy_source "$path")"; then
    warn "could not copy report; generated CSV is not readable: $(display_path "$path")"
    return 1
  fi

  source_abs="$(cd "$(dirname "$source")" && pwd -P)/$(basename "$source")"
  target="$output_dir/$(basename "$path")"
  if [[ "$source_abs" != "$target" ]] && ! cp -f "$source_abs" "$target"; then
    warn "failed to copy report to $(display_path "$target")"
    return 1
  fi
  normalize_output_report_permissions "$target"
}

normalize_output_report_permissions() {
  local target="$1"
  local target_dir
  local dir_owner
  local file_owner

  target_dir="$(dirname "$target")"
  dir_owner="$(stat -c '%u:%g' "$target_dir")" || {
    warn "failed to read output directory owner: $(display_path "$target_dir")"
    return 1
  }
  file_owner="$(stat -c '%u:%g' "$target")" || {
    warn "failed to read report owner: $(display_path "$target")"
    return 1
  }

  if [[ "$dir_owner" != "$file_owner" ]]; then
    if ! chown "$dir_owner" "$target" 2>/dev/null; then
      warn "failed to update report owner: $(display_path "$target")"
      return 1
    fi
  fi

  if ! chmod 664 "$target" 2>/dev/null; then
    warn "failed to update report permissions: $(display_path "$target")"
    return 1
  fi
}

prepare_output_dir() {
  [[ -n "$output_dir" ]] || return 0

  output_dir="$(expand_home_path "$output_dir")"
  output_dir="$(translate_linux_host_path_for_container "$output_dir")"
  [[ "$output_dir" = /* ]] || die "-out_dir must be an absolute directory path."
  mkdir -p "$output_dir" || die "failed to create output directory: $(display_path "$output_dir")"
  [[ -d "$output_dir" ]] || die "output path is not a directory: $(display_path "$output_dir")"
  output_dir="$(cd "$output_dir" && pwd -P)"
}

cancel_run() {
  local thread_id="$1"
  local run_id="$2"

  curl -fsS -X POST \
    --max-time 20 \
    "${LANGGRAPH_URL%/}/threads/$thread_id/runs/$run_id/cancel?wait=true&action=rollback" \
    >/dev/null 2>&1 || true
}

latest_run_id_for_thread() {
  local thread_id="$1"
  local response
  local run_id

  response="$(curl -fsS --max-time 5 \
    -H 'Accept: application/json' \
    "${LANGGRAPH_URL%/}/threads/$thread_id/runs?limit=1" 2>/dev/null || true)"
  run_id="$(printf '%s' "$response" | \
    "$response_parser_python" -m langgraph_server.response_parsing latest-run-id \
      2>/dev/null)" || return 1
  printf '%s' "$run_id"
}

cancel_thread_run() {
  local thread_id="$1"
  local run_id

  if run_id="$(latest_run_id_for_thread "$thread_id")"; then
    cancel_run "$thread_id" "$run_id"
  fi
}

cancel_unfinished_runs() {
  local reason="$1"
  local cancelled=0

  trap - INT TERM EXIT
  printf '\n%s，正在中止已提交的智能体分析...\n' "$reason" >&2

  for idx in "${!submitted_thread_ids[@]}"; do
    if [[ "${run_finished[$idx]:-0}" == "1" ]]; then
      continue
    fi
    printf 'cancel: %s thread=%s\n' \
      "${submitted_stems[$idx]}" \
      "${submitted_thread_ids[$idx]}" >&2
    cancel_thread_run "${submitted_thread_ids[$idx]}"
    if [[ -n "${run_pids[$idx]:-}" ]]; then
      kill "${run_pids[$idx]}" >/dev/null 2>&1 || true
    fi
    cancelled=$((cancelled + 1))
  done

  printf '已发送 %s 个 cancel 请求。\n' "$cancelled" >&2
}

handle_interrupt() {
  cancel_unfinished_runs "收到 Ctrl+C"
  exit 130
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
  local input_message_id="$5"
  local metadata context

  metadata="$(build_metadata_json "$user_id" "$authenticated")"
  context="$(build_context_json "$thread_id" "$user_id" "$authenticated")"
  printf '{"assistant_id":"%s","input":{"messages":[{"role":"user","content":"%s","id":"%s"}]},"metadata":%s,"context":%s,"if_not_exists":"create","config":{"recursion_limit":%s},"on_disconnect":"cancel","durability":"exit"}' \
    "$(json_escape "$LANGGRAPH_ASSISTANT")" \
    "$(json_escape "$prompt")" \
    "$(json_escape "$input_message_id")" \
    "$metadata" \
    "$context" \
    "$LANGGRAPH_RECURSION_LIMIT"
}

wait_run_to_file() {
  local thread_id="$1"
  local run_body="$2"
  local response_file="$3"
  local status_file="$4"
  local status=0

  if post_json_to_file "/threads/$thread_id/runs/wait" "$run_body" "$LINT_AGENT_BATCH_TIMEOUT" "$response_file"; then
    status=0
  else
    status=$?
  fi
  printf '%s\n' "$status" >"$status_file"
}

start_job() {
  local index="$1"
  local csv_path="${csv_paths[index]}"
  local source_path="${source_paths[index]}"
  local stem="${stems[index]}"
  local thread_id
  local input_message_id
  local response_file
  local request_file
  local status_file
  local prompt
  local thread_body
  local run_body

  thread_id="$(new_uuid)"
  input_message_id="$(new_uuid)"
  response_file="$log_dir/${stem}.${thread_id}.response.json"
  request_file="$log_dir/${stem}.${thread_id}.request.json"
  status_file="$log_dir/${stem}.${thread_id}.status"
  prompt="${csv_path} 为 lint 报告路径，${source_path} 为 Verilog 源代码包或源码目录路径，运行根因分析工作流，生成根因分析 CSV。"

  printf '\n[%s/%s] %s\n' "$((index + 1))" "$task_count" "$stem"
  printf 'thread_id: %s\n' "$thread_id"
  printf 'lint: %s\n' "$(display_path "$csv_path")"
  printf 'source: %s\n' "$(display_path "$source_path")"

  thread_body="$(build_thread_json "$thread_id" "$user_id" "$authenticated")"
  if ! post_json "/threads" "$thread_body" 30 >/dev/null; then
    warn "failed to pre-create thread metadata for $stem; will still submit the run."
  fi

  run_body="$(build_run_json "$thread_id" "$user_id" "$authenticated" "$prompt" "$input_message_id")"
  printf '%s\n' "$run_body" >"$request_file"

  submitted_thread_ids[index]="$thread_id"
  submitted_stems[index]="$stem"
  run_finished[index]=0
  job_response_files[index]="$response_file"
  job_status_files[index]="$status_file"
  job_input_message_ids[index]="$input_message_id"
  job_finalized[index]=0

  wait_run_to_file "$thread_id" "$run_body" "$response_file" "$status_file" &
  run_pids[index]=$!
  active_jobs=$((active_jobs + 1))
  printf 'submitted: %s active=%s/%s\n' "$stem" "$active_jobs" "$batch_jobs"
}

finalize_done_jobs() {
  local index
  local status
  local finalized_any=1
  local response_file
  local status_file
  local report_path

  for index in "${!run_pids[@]}"; do
    [[ "${job_finalized[index]:-0}" == "1" ]] && continue
    status_file="${job_status_files[index]:-}"
    [[ -n "$status_file" && -f "$status_file" ]] || continue

    IFS= read -r status <"$status_file" || status=1
    job_finalized[index]=1
    run_finished[index]=1
    active_jobs=$((active_jobs - 1))
    finalized_any=0

    if [[ "$status" != "0" ]]; then
      warn "agent run failed for ${submitted_stems[index]}. Response log: $(display_path "${job_response_files[index]}")"
      cancel_thread_run "${submitted_thread_ids[index]}"
      failure_count=$((failure_count + 1))
      continue
    fi

    response_file="${job_response_files[index]}"
    if ! report_path="$(parse_report_path_from_response \
      "$response_file" "${job_input_message_ids[index]}")"; then
      warn "invalid or incomplete agent response for ${submitted_stems[index]}. Response log: $(display_path "$response_file")"
      failure_count=$((failure_count + 1))
      continue
    fi

    printf '\ncompleted: %s\n' "${submitted_stems[index]}"
    printf 'assistant response saved: %s\n' "$(display_path "$response_file")"
    print_report_path "$report_path"
    if ! copy_report_path_to_output_dir "$report_path"; then
      failure_count=$((failure_count + 1))
      continue
    fi
    success_count=$((success_count + 1))
  done

  return "$finalized_any"
}

if [[ -z "${BASH_VERSION:-}" ]]; then
  die "this script must be run with bash."
fi
parse_args "$@"

require_command curl
require_command find
require_command sort
require_command date
require_command basename
require_command dirname
require_command cp
require_command mkdir
require_command stat
require_command chown
require_command chmod
require_command tr
if command -v python3 >/dev/null 2>&1; then
  response_parser_python="python3"
elif command -v python >/dev/null 2>&1; then
  response_parser_python="python"
else
  die "python3 or python is required to parse Agent Server responses."
fi

[[ "$LANGGRAPH_RECURSION_LIMIT" =~ ^[0-9]+$ ]] || die "LANGGRAPH_RECURSION_LIMIT must be an integer."
[[ "$LINT_AGENT_BATCH_TIMEOUT" =~ ^[0-9]+$ ]] || die "LINT_AGENT_BATCH_TIMEOUT must be an integer number of seconds."
[[ "$batch_jobs" =~ ^[1-9][0-9]*$ ]] || die "--jobs must be a positive integer."
prepare_output_dir

printf '请输入源码包/源码目录和 lint 报告所在文件夹绝对路径: '
IFS= read -r input_dir || die "failed to read input directory."

input_dir="$(expand_home_path "$input_dir")"
input_dir="$(translate_linux_host_path_for_container "$input_dir")"

[[ "$input_dir" = /* ]] || die "please enter an absolute directory path."
[[ -d "$input_dir" ]] || die "directory does not exist: $(display_path "$input_dir")"
input_dir="$(cd "$input_dir" && pwd -P)"
display_input_dir="$(display_path "$input_dir")"

csv_paths=()
source_paths=()
stems=()
missing_sources=()
missing_csvs=()
ambiguous_sources=()

while IFS= read -r -d '' csv_path; do
  csv_name="$(basename "$csv_path")"
  [[ "$csv_name" == *_root_cause*.csv ]] && continue

  stem="${csv_name%.csv}"
  collect_source_candidates "$stem"
  if [[ "${#source_candidates[@]}" -eq 0 ]]; then
    missing_sources+=("$csv_name")
    continue
  fi
  if [[ "${#source_candidates[@]}" -gt 1 ]]; then
    ambiguous_sources+=("$csv_name -> $(format_candidates "${source_candidates[@]}")")
    continue
  fi

  csv_paths+=("$csv_path")
  source_paths+=("${source_candidates[0]}")
  stems+=("$stem")
done < <(find "$input_dir" -maxdepth 1 -type f -name '*.csv' -print0 | sort -z)

while IFS= read -r -d '' source_path; do
  source_name="$(basename "$source_path")"
  if stem="$(archive_stem "$source_name")"; then
    [[ -f "$input_dir/$stem.csv" ]] || missing_csvs+=("$source_name")
  fi
done < <(find "$input_dir" -maxdepth 1 -type f -print0 | sort -z)

for item in "${missing_sources[@]}"; do
  warn "skip $item: matching source archive or source directory not found."
done
for item in "${missing_csvs[@]}"; do
  warn "skip $item: matching CSV report not found."
done
for item in "${ambiguous_sources[@]}"; do
  warn "skip ambiguous source candidates: $item"
done

task_count="${#csv_paths[@]}"
[[ "$task_count" -gt 0 ]] || die "no matched lint report + source archive/directory pairs found in $display_input_dir."

printf '找到 %s 组同名 lint 报告和 Verilog 源码输入。\n' "$task_count"
printf '智能体版本: %s\n' "$AGENT_DISPLAY_VERSION"
printf '并发任务数: %s\n' "$batch_jobs"
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
submitted_thread_ids=()
submitted_stems=()
run_finished=()
run_pids=()
job_response_files=()
job_status_files=()
job_input_message_ids=()
job_finalized=()
active_jobs=0
next_index=0

trap handle_interrupt INT TERM

while [[ "$next_index" -lt "$task_count" || "$active_jobs" -gt 0 ]]; do
  while [[ "$active_jobs" -lt "$batch_jobs" && "$next_index" -lt "$task_count" ]]; do
    start_job "$next_index" || true
    next_index=$((next_index + 1))
  done

  finalize_done_jobs || true
  if [[ "$active_jobs" -gt 0 ]]; then
    wait -n || true
    finalize_done_jobs || true
  fi
done
wait || true

printf '\n批处理完成：成功 %s，失败 %s。\n' "$success_count" "$failure_count"
printf '响应日志目录：%s\n' "$(display_path "$log_dir")"

if [[ "$failure_count" -gt 0 ]]; then
  exit 1
fi
