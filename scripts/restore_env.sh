#!/bin/bash

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

echo "Restoring system to default state..."

if command -v systemctl &> /dev/null; then
    echo "Starting irqbalance..."
    systemctl start irqbalance 2>/dev/null || true
fi

echo "Resetting IRQ affinity to all cores..."
for file in /proc/irq/*/smp_affinity; do
    if [ -w "$file" ]; then
        echo "f" > "$file" 2>/dev/null || true
    fi
done

PREFERRED_GOV="ondemand"

if ! grep -q "$PREFERRED_GOV" /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null; then
    PREFERRED_GOV="powersave"
fi

if [ -d "/sys/devices/system/cpu/cpu0/cpufreq" ]; then
    echo "Setting CPU governor to '$PREFERRED_GOV'..."
    for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo "$PREFERRED_GOV" > "$cpu"
    done
fi

echo "System restored. (Note: A reboot ensures a completely clean slate)"
