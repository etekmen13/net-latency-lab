#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi

ROLE=${1:-}
INTERFACE=${2:-eth0}
SNAPSHOT=${3:-/var/tmp/net-latency-lab-tuning}
NETWORK_CPU=${NETWORK_CPU:-0}
HOUSEKEEPING_CPU=${HOUSEKEEPING_CPU:-1}
WORKER_CPU=${WORKER_CPU:-2}
RECEIVER_CPU=${RECEIVER_CPU:-3}
SOCKET_MAX=${SOCKET_MAX:-4194304}
PROJECT_ROOT=${PROJECT_ROOT:-$(pwd)}
BUILD_SUBDIR=${BUILD_SUBDIR:-pi4-release}

if [[ ${ROLE} != receiver && ${ROLE} != sender ]]; then
  echo "Usage: sudo $0 receiver|sender [interface] [snapshot-directory]" >&2
  exit 2
fi
if [[ -e ${SNAPSHOT} ]]; then
  echo "Snapshot already exists: ${SNAPSHOT}" >&2
  exit 1
fi
mkdir -p "${SNAPSHOT}/governors" "${SNAPSHOT}/irq"

sysctl -n net.core.rmem_max > "${SNAPSHOT}/rmem_max"
sysctl -n net.core.wmem_max > "${SNAPSHOT}/wmem_max"
sysctl -n kernel.perf_event_paranoid > "${SNAPSHOT}/perf_event_paranoid"
sysctl -n kernel.kptr_restrict > "${SNAPSHOT}/kptr_restrict"
systemctl is-active irqbalance > "${SNAPSHOT}/irqbalance" 2>/dev/null || true
iw dev wlan0 get power_save > "${SNAPSHOT}/wifi_power" 2>/dev/null || true

for governor in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  [[ -r ${governor} ]] || continue
  cpu=$(basename "$(dirname "${governor}")")
  cp "${governor}" "${SNAPSHOT}/governors/${cpu}"
  echo performance > "${governor}"
done

systemctl stop irqbalance 2>/dev/null || true
sysctl -w "net.core.rmem_max=${SOCKET_MAX}"
sysctl -w "net.core.wmem_max=${SOCKET_MAX}"
sysctl -w kernel.perf_event_paranoid=-1
sysctl -w kernel.kptr_restrict=0
iw dev wlan0 set power_save off 2>/dev/null || true

awk -v names="${INTERFACE}|wlan" '$0 ~ names {gsub(":", "", $1); print $1}' /proc/interrupts |
while read -r irq; do
  [[ -n ${irq} && -w /proc/irq/${irq}/smp_affinity_list ]] || continue
  cp "/proc/irq/${irq}/smp_affinity_list" "${SNAPSHOT}/irq/${irq}"
  echo "${NETWORK_CPU}" > "/proc/irq/${irq}/smp_affinity_list"
done

for cpu in "${NETWORK_CPU}" "${HOUSEKEEPING_CPU}" "${WORKER_CPU}" "${RECEIVER_CPU}"; do
  taskset -c "${cpu}" true || { echo "CPU ${cpu} is unavailable for affinity" >&2; exit 1; }
done

if [[ ${ROLE} == receiver ]]; then
  : > "${SNAPSHOT}/capabilities"
  for binary in receiver_baseline receiver_batched receiver_threaded; do
    path="${PROJECT_ROOT}/build/${BUILD_SUBDIR}/${binary}"
    [[ -x ${path} ]] || { echo "Missing final receiver binary: ${path}" >&2; exit 1; }
    previous=$(getcap -n "${path}" | cut -d' ' -f2-)
    printf '%s\t%s\n' "${path}" "${previous}" >> "${SNAPSHOT}/capabilities"
    setcap cap_sys_nice=ep "${path}"
    getcap "${path}" | grep -q cap_sys_nice || { echo "Capability verification failed: ${path}" >&2; exit 1; }
  done
fi

cat > "${SNAPSHOT}/parameters" <<EOF
role=${ROLE}
interface=${INTERFACE}
network_cpu=${NETWORK_CPU}
housekeeping_cpu=${HOUSEKEEPING_CPU}
worker_cpu=${WORKER_CPU}
receiver_cpu=${RECEIVER_CPU}
socket_max=${SOCKET_MAX}
project_root=${PROJECT_ROOT}
build_subdir=${BUILD_SUBDIR}
EOF

for affinity in "${SNAPSHOT}"/irq/*; do
  [[ -f ${affinity} ]] || continue
  irq=$(basename "${affinity}")
  observed=$(cat "/proc/irq/${irq}/smp_affinity_list")
  [[ ${observed} == "${NETWORK_CPU}" ]] || { echo "IRQ ${irq} affinity verification failed" >&2; exit 1; }
done

echo "Saved restorable state in ${SNAPSHOT}. CPUs: network=${NETWORK_CPU}, housekeeping=${HOUSEKEEPING_CPU}, worker=${WORKER_CPU}, receive/sender=${RECEIVER_CPU}."
