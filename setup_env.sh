#!/bin/bash


set -e # exit shell immediately if any command returns a non-zero exit status.


if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit
fi

echo "Setting CPU governor to performance"
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  echo "performance" > "$cpu"
done


echo "Preventing Interrupt Requests from Core 3"

for file in /proc/irq/*/smp_affinity; do
  # Mask 7 = 0111 allows cores 0,1,2, but not 3
  echo "7" > "$file" 2>/dev/null || true
done


echo "Configuration complete."
