#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
BUILD_SUBDIR=${BUILD_SUBDIR:-pi4-release}
PORT=${NLL_SMOKE_PORT:-49390}
KEEP_ARTIFACTS=${KEEP_SMOKE_ARTIFACTS:-0}
RECEIVER=${PROJECT_ROOT}/build/${BUILD_SUBDIR}/receiver_baseline
SENDER=${PROJECT_ROOT}/build/${BUILD_SUBDIR}/sender
SMOKE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/nll-profile-smoke.XXXXXX")
wrapper_pid=
receiver_pid=

pid_alive() {
  kill -0 "$1" 2>/dev/null
}

wait_dead() {
  local pid=$1
  local attempts=${2:-100}
  local count
  for ((count = 0; count < attempts; ++count)); do
    pid_alive "${pid}" || return 0
    sleep 0.05
  done
  return 1
}

cleanup_processes() {
  set +e
  if [[ -n ${receiver_pid} ]] && pid_alive "${receiver_pid}"; then
    kill -INT "${receiver_pid}" 2>/dev/null
    wait_dead "${receiver_pid}" 40 || kill -TERM "${receiver_pid}" 2>/dev/null
    wait_dead "${receiver_pid}" 40 || kill -KILL "${receiver_pid}" 2>/dev/null
  fi
  if [[ -n ${wrapper_pid} ]] && pid_alive "${wrapper_pid}"; then
    wait_dead "${wrapper_pid}" 40 || kill -TERM "${wrapper_pid}" 2>/dev/null
    wait_dead "${wrapper_pid}" 40 || kill -KILL "${wrapper_pid}" 2>/dev/null
  fi
  if [[ -n ${wrapper_pid} ]]; then
    wait "${wrapper_pid}" 2>/dev/null
  fi
}

finish() {
  cleanup_processes
  if [[ ${KEEP_ARTIFACTS} == 1 ]]; then
    printf 'Smoke artifacts retained in %s\n' "${SMOKE_DIR}"
  else
    rm -rf -- "${SMOKE_DIR}"
  fi
}
trap finish EXIT INT TERM

[[ -x ${RECEIVER} ]] || { echo "Missing receiver: ${RECEIVER}" >&2; exit 1; }
[[ -x ${SENDER} ]] || { echo "Missing sender: ${SENDER}" >&2; exit 1; }
command -v perf >/dev/null || { echo "perf is unavailable" >&2; exit 1; }

run_smoke() {
  local mode=$1
  local run_dir=${SMOKE_DIR}/${mode}
  local workload_pid_file=${run_dir}/receiver.pid
  local trace=${run_dir}/receiver.bin
  local receiver_stats=${run_dir}/receiver.json
  local sender_stats=${run_dir}/sender.json
  local profile_artifact
  local profile_log=${run_dir}/profile.log
  local report=${run_dir}/perf-report.txt
  local shim
  local -a receiver_command profile_command
  mkdir -p "${run_dir}"
  profile_artifact=${run_dir}/$( [[ ${mode} == stat ]] && printf 'perf.csv' || printf 'perf.data' )
  receiver_command=("${RECEIVER}" --output "${trace}" --stats "${receiver_stats}"
                    --port "${PORT}" --scheduler other --priority 0 --sample-every 1)
  shim='pid_file=$1; shift; tmp=${pid_file}.tmp; printf "%s" "$$" > "$tmp"; mv -f -- "$tmp" "$pid_file"; exec "$@"'
  if [[ ${mode} == stat ]]; then
    profile_command=(perf stat -x, -o "${profile_artifact}" -e task-clock --
                     sh -c "${shim}" nll-workload "${workload_pid_file}"
                     "${receiver_command[@]}")
  else
    profile_command=(perf record -o "${profile_artifact}" -F 99 -g --call-graph fp --
                     sh -c "${shim}" nll-workload "${workload_pid_file}"
                     "${receiver_command[@]}")
  fi

  setsid "${profile_command[@]}" >"${profile_log}" 2>&1 &
  wrapper_pid=$!
  for _ in {1..500}; do
    [[ -s ${workload_pid_file} ]] && break
    pid_alive "${wrapper_pid}" || { wait "${wrapper_pid}"; return 1; }
    sleep 0.01
  done
  [[ -s ${workload_pid_file} ]] || { echo "${mode}: receiver PID was not published" >&2; return 1; }
  receiver_pid=$(<"${workload_pid_file}")
  [[ ${receiver_pid} =~ ^[1-9][0-9]*$ ]] || { echo "${mode}: invalid receiver PID" >&2; return 1; }
  sleep 0.25
  pid_alive "${receiver_pid}" || { echo "${mode}: receiver exited during startup" >&2; return 1; }

  "${SENDER}" --ip 127.0.0.1 --port "${PORT}" --rate 10000 --duration 0.5 \
    --mode steady --burst 1 --payload-size 64 --stats "${sender_stats}"
  kill -INT "${receiver_pid}"
  set +e
  wait "${wrapper_pid}"
  local status=$?
  set -e

  [[ ${status} -eq 0 ]] || { echo "${mode}: wrapper exited ${status}" >&2; return 1; }
  if pid_alive "${receiver_pid}"; then
    echo "${mode}: receiver survived wrapper exit" >&2
    return 1
  fi
  if pid_alive "${wrapper_pid}"; then
    echo "${mode}: perf wrapper survived wait" >&2
    return 1
  fi
  [[ -s ${receiver_stats} ]] || { echo "${mode}: receiver stats missing or empty" >&2; return 1; }
  [[ -s ${trace} ]] || { echo "${mode}: receiver trace missing or empty" >&2; return 1; }
  [[ -s ${profile_artifact} ]] || { echo "${mode}: perf artifact missing or empty" >&2; return 1; }
  python3 -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "${receiver_stats}"
  if [[ ${mode} == record ]]; then
    perf report --stdio -i "${profile_artifact}" >"${report}"
    [[ -s ${report} ]] || { echo "record: perf report is empty" >&2; return 1; }
  fi
  wrapper_pid=
  receiver_pid=
  printf '%s smoke passed with status 0\n' "${mode}"
}

run_smoke stat
run_smoke record
echo "All profile shutdown smokes passed"
