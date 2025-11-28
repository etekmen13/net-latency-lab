#!/bin/bash
set -e 

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

if [ -d "/sys/devices/system/cpu/cpu0/cpufreq" ]; then
    echo "Setting CPU governor to performance..."
    for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo "performance" > "$cpu"
    done
else
    echo "NOTICE: CPU frequency scaling not detected (WSL/VM?). Skipping governor tuning."
fi

echo "Preventing Interrupt Requests from Core 3..."

if grep -q "Microsoft" /proc/version; then
    echo "NOTICE: Running in WSL. IRQ pinning may not affect physical hardware."
fi

for file in /proc/irq/*/smp_affinity; do
    if [ -w "$file" ]; then
        # Mask 7 = 0111 allows cores 0,1,2, but not 3
        echo "7" > "$file" 2>/dev/null || true
    fi
done

echo "Configuration complete."
