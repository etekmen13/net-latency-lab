#!/usr/bin/env bash
set -uo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi
SNAPSHOT=${1:-/var/tmp/net-latency-lab-tuning}
[[ -d ${SNAPSHOT} ]] || { echo "Snapshot not found: ${SNAPSHOT}" >&2; exit 1; }

# Restoration is deliberately best effort: one failing item must never leave the
# remaining tuning applied.  Every failure is reported and the script exits
# nonzero so the operator can retry or correct the machine by hand.
FAILURES=0
attempt() {
  local description=$1
  shift
  "$@" >/dev/null || { echo "restore failed: ${description}" >&2; FAILURES=$((FAILURES + 1)); }
}

for key in net.core.rmem_max net.core.wmem_max kernel.perf_event_paranoid \
           kernel.kptr_restrict kernel.sched_rt_runtime_us; do
  saved="${SNAPSHOT}/${key##*.}"
  [[ -s ${saved} ]] || { echo "restore skipped (no snapshot): ${key}" >&2; FAILURES=$((FAILURES + 1)); continue; }
  attempt "${key}" sysctl -w "${key}=$(cat "${saved}")"
done

for saved in "${SNAPSHOT}"/governors/*; do
  [[ -f ${saved} ]] || continue
  cpu=$(basename "${saved}")
  target="/sys/devices/system/cpu/${cpu}/cpufreq/scaling_governor"
  [[ -w ${target} ]] || continue
  cat "${saved}" > "${target}" ||
    { echo "restore failed: governor ${cpu}" >&2; FAILURES=$((FAILURES + 1)); }
done

if [[ -f ${SNAPSHOT}/capabilities ]]; then
  while IFS=$'\t' read -r binary capability; do
    [[ -n ${binary} && -e ${binary} ]] || continue
    setcap -r "${binary}" 2>/dev/null
    [[ -n ${capability} ]] || continue
    attempt "capability ${capability} on ${binary}" setcap "${capability}" "${binary}"
  done < "${SNAPSHOT}/capabilities"
fi

for saved in "${SNAPSHOT}"/irq/*; do
  [[ -f ${saved} ]] || continue
  irq=$(basename "${saved}")
  target="/proc/irq/${irq}/smp_affinity_list"
  [[ -w ${target} ]] || continue
  cat "${saved}" > "${target}" ||
    { echo "restore failed: IRQ ${irq} affinity" >&2; FAILURES=$((FAILURES + 1)); }
done

if [[ -s ${SNAPSHOT}/pause && -s ${SNAPSHOT}/bench_interface ]]; then
  interface=$(cat "${SNAPSHOT}/bench_interface")
  autoneg=$(awk '/^Autonegotiate:/ {print $2}' "${SNAPSHOT}/pause")
  rx=$(awk '/^RX:/ {print $2}' "${SNAPSHOT}/pause")
  tx=$(awk '/^TX:/ {print $2}' "${SNAPSHOT}/pause")
  attempt "pause frames on ${interface}" \
    ethtool -A "${interface}" autoneg "${autoneg:-on}" rx "${rx:-on}" tx "${tx:-on}"
fi

if grep -q '^active' "${SNAPSHOT}/irqbalance" 2>/dev/null; then
  attempt "irqbalance restart" systemctl start irqbalance
fi
if grep -qi 'power save: on' "${SNAPSHOT}/wifi_power" 2>/dev/null; then
  attempt "wlan0 power save" iw dev wlan0 set power_save on
fi

if ((FAILURES > 0)); then
  echo "Restored ${SNAPSHOT} with ${FAILURES} failure(s); correct them before publishing." >&2
  exit 1
fi
echo "Restored tuning state from ${SNAPSHOT}. Keep the snapshot for audit/recovery."
