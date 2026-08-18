#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PREFLIGHT_CONFIG=${PROJECT_ROOT}/results/raw/config-dietpi.yaml
PROFILE_CONFIG=${PROJECT_ROOT}/results/raw/profile-dietpi.yaml
DRY_RUN=0
CURRENT_STAGE="argument parsing"
LOG_DIR=

on_exit() {
  local status=$1
  if ((status != 0)); then
    printf 'PROFILE PHASE FAILED at stage: %s\n' "${CURRENT_STAGE}" >&2
    if [[ -n ${LOG_DIR} ]]; then
      printf 'Retained logs and diagnostics: %s\n' "${LOG_DIR}" >&2
    fi
  fi
}
trap 'on_exit $?' EXIT

usage() {
  cat <<'EOF'
Usage: scripts/run_profile_phase.sh [OPTIONS]

Deploy and test the frozen benchmark commit, run distributed preflight, execute
the fixed profile campaign, validate all 12 runs, and generate its summary.

Options:
  --preflight-config PATH  Measurement config used for preflight
  --profile-config PATH    Profile campaign config
  --dry-run                Perform local validation without SSH or mutations
  -h, --help               Show this help
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case $1 in
    --preflight-config)
      (($# >= 2)) || die "--preflight-config requires a path"
      PREFLIGHT_CONFIG=$2
      shift 2
      ;;
    --profile-config)
      (($# >= 2)) || die "--profile-config requires a path"
      PROFILE_CONFIG=$2
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

if [[ -n ${NLL_PYTHON:-} ]]; then
  PYTHON=${NLL_PYTHON}
else
  PYTHON=${PROJECT_ROOT}/.venv/bin/python
fi

CURRENT_STAGE="controller and configuration validation"
[[ -x ${PYTHON} ]] || die "Python environment is unavailable: ${PYTHON}"
[[ -f ${PREFLIGHT_CONFIG} ]] || die "Preflight config does not exist: ${PREFLIGHT_CONFIG}"
[[ -f ${PROFILE_CONFIG} ]] || die "Profile config does not exist: ${PROFILE_CONFIG}"
PREFLIGHT_CONFIG=$(cd "$(dirname "${PREFLIGHT_CONFIG}")" && pwd)/$(basename "${PREFLIGHT_CONFIG}")
PROFILE_CONFIG=$(cd "$(dirname "${PROFILE_CONFIG}")" && pwd)/$(basename "${PROFILE_CONFIG}")
cd "${PROJECT_ROOT}"
git -C "${PROJECT_ROOT}" rev-parse --is-inside-work-tree >/dev/null
if ! git -C "${PROJECT_ROOT}" diff --quiet --ignore-submodules -- ||
   ! git -C "${PROJECT_ROOT}" diff --cached --quiet --ignore-submodules --; then
  die "Controller tracked worktree is dirty; commit or restore tracked changes first"
fi

mapfile -t CONFIG_VALUES < <("${PYTHON}" - "${PREFLIGHT_CONFIG}" "${PROFILE_CONFIG}" <<'PY'
import re
import sys
from pathlib import Path

import yaml

paths = [Path(value) for value in sys.argv[1:]]
configs = []
for path in paths:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        global_config = config["global"]
        nodes = global_config["nodes"]
        runtime = global_config["runtime"]
        values = (
            global_config["benchmark_commit"], global_config["topology"],
            global_config["user"], nodes["receiver"]["management_host"],
            nodes["sender"]["management_host"],
            global_config["remote_project_root"], runtime["build_preset"],
        )
    except (OSError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise SystemExit(f"Invalid config {path}: {exc}")
    if any(not isinstance(value, str) or not value for value in values):
        raise SystemExit(f"Invalid config {path}: deployment fields must be nonempty strings")
    configs.append(values)

if configs[0] != configs[1]:
    labels = ("benchmark_commit", "topology", "user", "receiver host",
              "sender host", "remote_project_root", "build_preset")
    differences = [label for label, left, right in zip(labels, *configs)
                   if left != right]
    raise SystemExit("Configs disagree on: " + ", ".join(differences))

commit, topology, user, receiver, sender, remote_root, preset = configs[0]
if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("benchmark_commit must be a full lowercase 40-character SHA")
if topology != "distributed_ethernet":
    raise SystemExit("Both configs must use distributed_ethernet topology")
safe_token = re.compile(r"^[A-Za-z0-9_.:@-]+$")
for label, value in (("SSH user", user), ("receiver host", receiver),
                     ("sender host", sender), ("build preset", preset)):
    if not safe_token.fullmatch(value):
        raise SystemExit(f"Unsafe {label}: {value!r}")
if not remote_root.startswith("/") or any(character.isspace() for character in remote_root):
    raise SystemExit(f"remote_project_root must be an absolute whitespace-free path: {remote_root!r}")
for value in configs[0]:
    print(value)
PY
)
[[ ${#CONFIG_VALUES[@]} -eq 7 ]] || die "Could not read deployment settings from configs"
FROZEN_SHA=${CONFIG_VALUES[0]}
SSH_USER=${CONFIG_VALUES[2]}
RECEIVER_HOST=${CONFIG_VALUES[3]}
SENDER_HOST=${CONFIG_VALUES[4]}
REMOTE_ROOT=${CONFIG_VALUES[5]}
BUILD_PRESET=${CONFIG_VALUES[6]}

git -C "${PROJECT_ROOT}" cat-file -e "${FROZEN_SHA}^{commit}" 2>/dev/null ||
  die "Frozen benchmark commit does not exist locally: ${FROZEN_SHA}"
git -C "${PROJECT_ROOT}" show-ref --verify --quiet refs/remotes/origin/main ||
  die "origin/main is unavailable; fetch it explicitly before running"
git -C "${PROJECT_ROOT}" merge-base --is-ancestor "${FROZEN_SHA}" refs/remotes/origin/main ||
  die "Frozen benchmark commit is not reachable from origin/main: ${FROZEN_SHA}"
CONTROLLER_HEAD=$(git -C "${PROJECT_ROOT}" rev-parse HEAD)
[[ ${FROZEN_SHA} != "${CONTROLLER_HEAD}" ]] ||
  die "Frozen benchmark commit must precede the controller tooling commit"

if ((DRY_RUN)); then
  printf 'Dry run passed local validation.\n'
  printf 'Frozen benchmark commit: %s\n' "${FROZEN_SHA}"
  printf 'Receiver deployment: %s@%s:%s (%s)\n' \
    "${SSH_USER}" "${RECEIVER_HOST}" "${REMOTE_ROOT}" "${BUILD_PRESET}"
  printf 'Sender deployment: %s@%s:%s (%s)\n' \
    "${SSH_USER}" "${SENDER_HOST}" "${REMOTE_ROOT}" "${BUILD_PRESET}"
  printf 'Preflight config: %s\nProfile config: %s\n' \
    "${PREFLIGHT_CONFIG}" "${PROFILE_CONFIG}"
  exit 0
fi

RUN_STAMP=$(date -u +%Y%m%dT%H%M%S_%NZ)
LOG_DIR=${PROJECT_ROOT}/results/profile-phase/${RUN_STAMP}
mkdir -p "${LOG_DIR}"
RECEIVER_LOG=${LOG_DIR}/receiver-deployment.log
SENDER_LOG=${LOG_DIR}/sender-deployment.log
PREFLIGHT_REPORT=${LOG_DIR}/preflight-report.json
PREFLIGHT_LOG=${LOG_DIR}/preflight.log
PROFILE_LOG=${LOG_DIR}/profile-campaign.log

CURRENT_STAGE="SSH agent discovery"
agent_usable() {
  local candidate=$1
  [[ -n ${candidate} && -S ${candidate} ]] || return 1
  SSH_AUTH_SOCK=${candidate} ssh-add -l >/dev/null 2>&1
}

AGENT_SOCKET=${SSH_AUTH_SOCK:-}
if ! agent_usable "${AGENT_SOCKET}"; then
  TMUX_AGENT=$(tmux show-environment -g SSH_AUTH_SOCK 2>/dev/null || true)
  if [[ ${TMUX_AGENT} == SSH_AUTH_SOCK=* ]]; then
    AGENT_SOCKET=${TMUX_AGENT#SSH_AUTH_SOCK=}
  else
    AGENT_SOCKET=
  fi
fi
agent_usable "${AGENT_SOCKET}" ||
  die "No unlocked SSH agent found; unlock it and refresh tmux SSH_AUTH_SOCK"
export SSH_AUTH_SOCK=${AGENT_SOCKET}

SSH_OPTIONS=(-S none -o BatchMode=yes -o ConnectTimeout=10)

remote_readiness() {
  local role=$1 host=$2 log=$3
  {
    printf '[%s] Checking %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${role}" "${host}"
    ssh "${SSH_OPTIONS[@]}" "${SSH_USER}@${host}" bash -s -- "${REMOTE_ROOT}" <<'REMOTE'
set -euo pipefail
project_root=$1
cd "${project_root}"
[[ -z $(git status --porcelain) ]] || { echo "Remote checkout is dirty" >&2; exit 1; }
survivors=
for comm_path in /proc/[0-9]*/comm; do
  [[ -r ${comm_path} ]] || continue
  IFS= read -r comm <"${comm_path}" || continue
  case ${comm} in
    perf|sender|receiver_*) survivors+="${comm_path%/comm} ${comm}"$'\n' ;;
  esac
done
[[ -z ${survivors} ]] || { printf 'Live benchmark processes:\n%s' "${survivors}" >&2; exit 1; }
printf 'Remote readiness passed at commit %s\n' "$(git rev-parse HEAD)"
REMOTE
  } >>"${log}" 2>&1
}

wait_for_pair() {
  local receiver_pid=$1 sender_pid=$2 receiver_status sender_status
  set +e
  wait "${receiver_pid}"
  receiver_status=$?
  wait "${sender_pid}"
  sender_status=$?
  set -e
  if ((receiver_status != 0 || sender_status != 0)); then
    printf 'Receiver status: %d; sender status: %d\n' \
      "${receiver_status}" "${sender_status}" >&2
    return 1
  fi
}

CURRENT_STAGE="remote readiness validation"
remote_readiness receiver "${RECEIVER_HOST}" "${RECEIVER_LOG}" &
RECEIVER_JOB=$!
remote_readiness sender "${SENDER_HOST}" "${SENDER_LOG}" &
SENDER_JOB=$!
wait_for_pair "${RECEIVER_JOB}" "${SENDER_JOB}"

deploy_host() {
  local role=$1 host=$2 log=$3
  {
    printf '[%s] Deploying %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${role}" "${host}"
    ssh "${SSH_OPTIONS[@]}" "${SSH_USER}@${host}" bash -s -- \
      "${REMOTE_ROOT}" "${FROZEN_SHA}" "${BUILD_PRESET}" "${role}" <<'REMOTE'
set -euo pipefail
project_root=$1
frozen_sha=$2
build_preset=$3
role=$4
cd "${project_root}"
git fetch origin
git checkout --detach "${frozen_sha}"
[[ $(git rev-parse HEAD) == "${frozen_sha}" ]]
[[ -z $(git status --porcelain) ]]
cmake --preset "${build_preset}"
cmake --build --preset "${build_preset}" -j2
ctest --preset "${build_preset}"
.venv/bin/python -m pytest -q
cmake --preset tsan
cmake --build --preset tsan -j2
ctest --preset tsan
if [[ ${role} == receiver ]]; then
  for receiver in build/"${build_preset}"/receiver_baseline \
                  build/"${build_preset}"/receiver_batched \
                  build/"${build_preset}"/receiver_threaded; do
    setcap cap_sys_nice=ep "${receiver}"
    getcap "${receiver}" | grep -q 'cap_sys_nice=ep'
  done
  for repetition in $(seq 1 20); do
    NLL_THREADED_RECEIVER_BINARY=build/"${build_preset}"/receiver_threaded \
      .venv/bin/python -m pytest -q tests/test_loopback.py \
      -k threaded_fifo_affinity_lifecycle
  done
  # The smoke keeps its own artifacts automatically when it fails.
  BUILD_SUBDIR="${build_preset}" ./scripts/profile_shutdown_smoke.sh
fi
[[ $(git rev-parse HEAD) == "${frozen_sha}" ]]
[[ -z $(git status --porcelain) ]]
printf 'Deployment and verification passed for %s at %s\n' "${role}" "${frozen_sha}"
REMOTE
  } >>"${log}" 2>&1
}

CURRENT_STAGE="concurrent Pi deployment and tests"
deploy_host receiver "${RECEIVER_HOST}" "${RECEIVER_LOG}" &
RECEIVER_JOB=$!
deploy_host sender "${SENDER_HOST}" "${SENDER_LOG}" &
SENDER_JOB=$!
wait_for_pair "${RECEIVER_JOB}" "${SENDER_JOB}"

CURRENT_STAGE="distributed preflight"
"${PROJECT_ROOT}/run_lab.sh" --config "${PREFLIGHT_CONFIG}" --preflight \
  --preflight-report "${PREFLIGHT_REPORT}" 2>&1 | tee "${PREFLIGHT_LOG}"
[[ -s ${PREFLIGHT_REPORT} ]] || die "Preflight report is missing or empty"

CURRENT_STAGE="profile campaign"
"${PROJECT_ROOT}/run_lab.sh" --config "${PROFILE_CONFIG}" --skip-build \
  2>&1 | tee "${PROFILE_LOG}"
mapfile -t COMPLETED_SESSIONS < <(
  sed -n 's/^Completed session: //p' "${PROFILE_LOG}"
)
[[ ${#COMPLETED_SESSIONS[@]} -eq 1 ]] ||
  die "Profile campaign printed ${#COMPLETED_SESSIONS[@]} completed sessions; expected exactly one"
PROFILE_SESSION=${COMPLETED_SESSIONS[0]}
[[ -d ${PROFILE_SESSION} ]] || die "Printed profile session does not exist: ${PROFILE_SESSION}"

CURRENT_STAGE="profile session validation"
VALIDATED_SESSION=$("${PYTHON}" "${PROJECT_ROOT}/analysis/profile_tools.py" validate \
  "${PROFILE_SESSION}")
[[ ${VALIDATED_SESSION} == "$(cd "${PROFILE_SESSION}" && pwd)" ]] ||
  die "Validator returned an unexpected session: ${VALIDATED_SESSION}"

CURRENT_STAGE="profile summary generation"
SUMMARY_PATH=$("${PYTHON}" "${PROJECT_ROOT}/analysis/profile_tools.py" summarize \
  "${VALIDATED_SESSION}")
[[ -s ${SUMMARY_PATH} ]] || die "Profile summary is missing or empty: ${SUMMARY_PATH}"

remote_process_recheck() {
  local role=$1 host=$2 log=$3
  {
    printf '[%s] Post-profile process check for %s %s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${role}" "${host}"
    ssh "${SSH_OPTIONS[@]}" "${SSH_USER}@${host}" bash -s <<'REMOTE'
set -euo pipefail
survivors=
for comm_path in /proc/[0-9]*/comm; do
  [[ -r ${comm_path} ]] || continue
  IFS= read -r comm <"${comm_path}" || continue
  case ${comm} in
    perf|sender|receiver_*) survivors+="${comm_path%/comm} ${comm}"$'\n' ;;
  esac
done
[[ -z ${survivors} ]] || { printf 'Surviving benchmark processes:\n%s' "${survivors}" >&2; exit 1; }
echo 'No surviving perf, sender, or receiver processes'
REMOTE
  } >>"${log}" 2>&1
}

CURRENT_STAGE="post-profile remote process validation"
remote_process_recheck receiver "${RECEIVER_HOST}" "${RECEIVER_LOG}" &
RECEIVER_JOB=$!
remote_process_recheck sender "${SENDER_HOST}" "${SENDER_LOG}" &
SENDER_JOB=$!
wait_for_pair "${RECEIVER_JOB}" "${SENDER_JOB}"

CURRENT_STAGE="complete"
printf 'Profile phase completed successfully.\n'
printf 'Frozen benchmark commit: %s\n' "${FROZEN_SHA}"
printf 'Preflight report: %s\n' "${PREFLIGHT_REPORT}"
printf 'Receiver deployment log: %s\n' "${RECEIVER_LOG}"
printf 'Sender deployment log: %s\n' "${SENDER_LOG}"
printf 'Profile campaign log: %s\n' "${PROFILE_LOG}"
printf 'Validated profile session: %s\n' "${VALIDATED_SESSION}"
printf 'Profile summary: %s\n' "${SUMMARY_PATH}"
