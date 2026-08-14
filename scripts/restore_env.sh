#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi
SNAPSHOT=${1:-/var/tmp/net-latency-lab-tuning}
[[ -d ${SNAPSHOT} ]] || { echo "Snapshot not found: ${SNAPSHOT}" >&2; exit 1; }

sysctl -w "net.core.rmem_max=$(cat "${SNAPSHOT}/rmem_max")"
sysctl -w "net.core.wmem_max=$(cat "${SNAPSHOT}/wmem_max")"
sysctl -w "kernel.perf_event_paranoid=$(cat "${SNAPSHOT}/perf_event_paranoid")"
sysctl -w "kernel.kptr_restrict=$(cat "${SNAPSHOT}/kptr_restrict")"
for saved in "${SNAPSHOT}"/governors/*; do
  [[ -f ${saved} ]] || continue
  cpu=$(basename "${saved}")
  target="/sys/devices/system/cpu/${cpu}/cpufreq/scaling_governor"
  [[ -w ${target} ]] && cat "${saved}" > "${target}"
done
if [[ -f ${SNAPSHOT}/capabilities ]]; then
  while IFS=$'\t' read -r binary capability; do
    [[ -n ${binary} && -e ${binary} ]] || continue
    setcap -r "${binary}" 2>/dev/null || true
    [[ -n ${capability} ]] && setcap "${capability}" "${binary}"
  done < "${SNAPSHOT}/capabilities"
fi
for saved in "${SNAPSHOT}"/irq/*; do
  [[ -f ${saved} ]] || continue
  irq=$(basename "${saved}")
  target="/proc/irq/${irq}/smp_affinity_list"
  [[ -w ${target} ]] && cat "${saved}" > "${target}"
done
grep -q '^active' "${SNAPSHOT}/irqbalance" 2>/dev/null && systemctl start irqbalance || true
grep -qi 'on' "${SNAPSHOT}/wifi_power" 2>/dev/null && iw dev wlan0 set power_save on || true
echo "Restored tuning state from ${SNAPSHOT}. Keep the snapshot for audit/recovery."
