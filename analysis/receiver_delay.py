import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
path = sys.argv[1]
# 1. Load Data
df = pd.read_csv(path)

# 2. Pre-process
# Convert ns to microseconds (us) for readability
df["latency_us"] = df["latency"] / 1000.0

# Calculate Jitter: The difference in delay between consecutive packets
# Formula: |Latency_n - Latency_{n-1}|
df["jitter_us"] = df["latency_us"].diff().abs()

# Filter out the first 5 packets (ARP/Cold Start artifacts) for statistics
stats_df = df.iloc[5:]

# 3. Print Statistics (for the report table)
print("=== Statistics (Excluding Warm-up) ===")
print(f"Count: {len(stats_df)}")
print(f"Median Raw Latency: {stats_df['latency_us'].median():.2f} us")
print(f"Mean Jitter:        {stats_df['jitter_us'].mean():.2f} us")
print(f"99th %ile Jitter:   {stats_df['jitter_us'].quantile(0.99):.2f} us")

# 4. Plotting
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)

# Plot A: Raw Latency (One-Way Delay)
ax1.plot(
    df["seq"],
    df["latency_us"],
    color="#1f77b4",
    alpha=0.8,
    linewidth=1,
    label="Measured OWD",
)
ax1.set_ylabel("Measured Latency (µs)")
ax1.set_title(
    "Figure 1: Raw One-Way Delay (OWD) Showing Clock Skew",
    fontsize=12,
    fontweight="bold",
)
ax1.grid(True, linestyle="--", alpha=0.6)
ax1.legend(loc="upper right")

# Add an annotation about the negative values
median_lat = stats_df["latency_us"].median()
ax1.axhline(
    y=median_lat, color="r", linestyle=":", label=f"Median ({median_lat:.1f}µs)"
)
ax1.text(
    len(df) * 0.02,
    median_lat + 10,
    f"Negative values due to\n~{-median_lat:.0f}µs Clock Offset (Pi-B behind Pi-A)",
    fontsize=9,
    color="red",
    backgroundcolor="white",
)

# Plot B: Jitter (Stability)
ax2.plot(df["seq"], df["jitter_us"], color="#ff7f0e", alpha=0.8, linewidth=0.8)
ax2.set_ylabel("Jitter (µs)")
ax2.set_xlabel("Sequence Number")
ax1.set_title(
    "Figure 2: Inter-Arrival Jitter (Stability)", fontsize=12, fontweight="bold"
)
ax2.grid(True, linestyle="--", alpha=0.6)
ax2.set_ylim(
    0, stats_df["jitter_us"].quantile(0.99) * 2
)  # Zoom in, ignore huge outliers

plt.tight_layout()
plt.savefig("data/latency_plot.png", dpi=299)
