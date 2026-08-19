#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi

ROLE=${1:-}
# Extended regular expression matched against /proc/interrupts lines.  Label
# conventions differ by kernel: the Raspberry Pi Foundation kernel used by DietPi
# v9.17.2 (6.18.x +rpt-rpi-v8) prints "eth0" for the NIC, while other kernels
# print the driver name "bcmgenet".  Always confirm against /proc/interrupts on
# the systems you are actually using rather than trusting either convention.
IRQ_PATTERN=${2:-eth0}
# Always steered alongside IRQ_PATTERN, whatever the operator passes.  These are
# the management-path and storage interrupts, which must be kept off the pinned
# measurement cores.  On the Pi 4 the Wi-Fi driver is SDIO-attached, so it raises
# no "wlan" line at all -- it shares the "mmc1, mmc0" line with the SD card, and
# that line was previously left with a 0-3 affinity mask, i.e. permitted to fire
# on the receiver and worker cores.  It happened to land on CPU0 in practice, but
# nothing enforced it.
HOUSEKEEPING_IRQ_PATTERN=${HOUSEKEEPING_IRQ_PATTERN:-wlan|mmc|brcm}
SNAPSHOT=${3:-/var/tmp/net-latency-lab-tuning}
# The benchmark link itself, for pause-frame control. Distinct from IRQ_PATTERN,
# which matches driver labels in /proc/interrupts.
BENCH_INTERFACE=${BENCH_INTERFACE:-eth0}
NETWORK_CPU=${NETWORK_CPU:-0}
HOUSEKEEPING_CPU=${HOUSEKEEPING_CPU:-1}
WORKER_CPU=${WORKER_CPU:-2}
RECEIVER_CPU=${RECEIVER_CPU:-3}
SOCKET_MAX=${SOCKET_MAX:-4194304}
PROJECT_ROOT=${PROJECT_ROOT:-$(pwd)}
BUILD_SUBDIR=${BUILD_SUBDIR:-pi4-release}

if [[ ${ROLE} != receiver && ${ROLE} != sender ]]; then
  echo "Usage: sudo $0 receiver|sender [irq-match-pattern] [snapshot-directory]" >&2
  exit 2
fi
if [[ -e ${SNAPSHOT} ]]; then
  echo "Snapshot already exists: ${SNAPSHOT}" >&2
  exit 1
fi
mkdir -p "${SNAPSHOT}/governors" "${SNAPSHOT}/irq"

COMPLETED=0
on_exit() {
  local status=$1
  ((status == 0 || COMPLETED == 1)) && return 0
  echo "setup_env.sh failed (status ${status}); tuning may be partially applied." >&2
  echo "Run 'sudo scripts/restore_env.sh ${SNAPSHOT}' and then remove ${SNAPSHOT} before retrying." >&2
}
trap 'on_exit $?' EXIT

sysctl -n net.core.rmem_max > "${SNAPSHOT}/rmem_max"
sysctl -n net.core.wmem_max > "${SNAPSHOT}/wmem_max"
sysctl -n kernel.perf_event_paranoid > "${SNAPSHOT}/perf_event_paranoid"
sysctl -n kernel.kptr_restrict > "${SNAPSHOT}/kptr_restrict"
systemctl is-active irqbalance > "${SNAPSHOT}/irqbalance" 2>/dev/null || true
iw dev wlan0 get power_save > "${SNAPSHOT}/wifi_power" 2>/dev/null || true
sysctl -n kernel.sched_rt_runtime_us > "${SNAPSHOT}/sched_rt_runtime_us"
printf '%s\n' "${BENCH_INTERFACE}" > "${SNAPSHOT}/bench_interface"
ethtool -a "${BENCH_INTERFACE}" > "${SNAPSHOT}/pause" 2>/dev/null || true

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

# A saturated SCHED_FIFO receiver is otherwise throttled for 50 ms of every
# second by the default 950000/1000000 RT bandwidth limit. That penalises the
# threaded receiver twice over, because its RX and worker threads are throttled
# independently, which would show up as an architecture difference.
sysctl -w kernel.sched_rt_runtime_us=-1

# 802.3x pause frames make the experiment closed-loop: the receiver's NIC
# back-pressures the sender at the link layer, so offered load stops being an
# independent variable and receiver overload is hidden as sender-side queueing
# delay instead of appearing as loss. Measured on this pair before disabling:
# receiver tx_pause 7,209,416 exactly matching sender rx_pause, with receiver
# rx_missed_errors 51,314.
if ! ethtool -A "${BENCH_INTERFACE}" autoneg off rx off tx off 2>/dev/null; then
  ethtool -A "${BENCH_INTERFACE}" rx off tx off
fi

awk -v names="${IRQ_PATTERN}|${HOUSEKEEPING_IRQ_PATTERN}" '$0 ~ names {gsub(":", "", $1); print $1}' /proc/interrupts |
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

# Verify the locally configured setting. The negotiated state can legitimately
# still read "on" until the link partner is configured too, so it is reported
# rather than enforced here; the harness gates on zero pause-frame deltas.
configured=$(ethtool -a "${BENCH_INTERFACE}" | awk '/^(RX|TX):/ {print $2}' | sort -u)
if [[ ${configured} != "off" ]]; then
  echo "Pause frames are still enabled on ${BENCH_INTERFACE}:" >&2
  ethtool -a "${BENCH_INTERFACE}" >&2
  exit 1
fi
if ethtool -a "${BENCH_INTERFACE}" | grep -q 'negotiated:[[:space:]]*on'; then
  echo "Note: pause is still negotiated on ${BENCH_INTERFACE}; run this script on the link partner too." >&2
fi
[[ $(sysctl -n kernel.sched_rt_runtime_us) == "-1" ]] || {
  echo "kernel.sched_rt_runtime_us did not take effect" >&2; exit 1; }

cat > "${SNAPSHOT}/parameters" <<EOF
role=${ROLE}
irq_pattern=${IRQ_PATTERN}
housekeeping_irq_pattern=${HOUSEKEEPING_IRQ_PATTERN}
bench_interface=${BENCH_INTERFACE}
network_cpu=${NETWORK_CPU}
housekeeping_cpu=${HOUSEKEEPING_CPU}
worker_cpu=${WORKER_CPU}
receiver_cpu=${RECEIVER_CPU}
socket_max=${SOCKET_MAX}
project_root=${PROJECT_ROOT}
build_subdir=${BUILD_SUBDIR}
EOF

steered=0
for affinity in "${SNAPSHOT}"/irq/*; do
  [[ -f ${affinity} ]] || continue
  irq=$(basename "${affinity}")
  observed=$(cat "/proc/irq/${irq}/smp_affinity_list")
  [[ ${observed} == "${NETWORK_CPU}" ]] || { echo "IRQ ${irq} affinity verification failed" >&2; exit 1; }
  steered=$((steered + 1))
done
if ((steered == 0)); then
  echo "No /proc/interrupts line matched '${IRQ_PATTERN}|${HOUSEKEEPING_IRQ_PATTERN}'; no IRQ was steered to CPU ${NETWORK_CPU}." >&2
  echo "Inspect /proc/interrupts for the labels your kernel actually prints. The Raspberry Pi Foundation kernel names the NIC lines 'eth0'; other kernels name them 'bcmgenet'." >&2
  exit 1
fi

COMPLETED=1
echo "Saved restorable state in ${SNAPSHOT}. Steered ${steered} IRQ line(s) matching '${IRQ_PATTERN}|${HOUSEKEEPING_IRQ_PATTERN}'."
echo "CPUs: network=${NETWORK_CPU}, housekeeping=${HOUSEKEEPING_CPU}, worker=${WORKER_CPU}, receive/sender=${RECEIVER_CPU}."
