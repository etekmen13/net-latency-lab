import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('filename')
parser.add_argument("-o","--output",type=str, required=True)

args = parser.parse_args()

df = pd.read_csv(args.filename)

df["latency_us"] = df["latency"] / 1000.0

df["jitter_us"] = df["latency_us"].diff().abs()

stats_df = df.iloc[5:]

lat_median = stats_df['latency_us'].median()
jit_mean = stats_df['jitter_us'].mean()
jit_99 = stats_df['jitter_us'].quantile(0.99)
print("=== Statistics (Excluding Warm-up) ===")
print(f"Count: {len(stats_df)}")
print(f"Median Raw Latency: {lat_median:.2f} us")
print(f"Mean Jitter:        {jit_mean:.2f} us")
print(f"99th %ile Jitter:   {jit_99:.2f} us")

# Construct DataFrame using a Dictionary
# NOTE: The brackets [] around variables are CRITICAL. 
# They tell Pandas "This is a list containing one row".
summary = pd.DataFrame({
    'latency_median': [lat_median],
    'jitter_mean':    [jit_mean],
    'jitter_99':      [jit_99]
})

summary.to_csv(args.output + "/summary.csv", index=False)
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)

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

ax2.plot(df["seq"], df["jitter_us"], color="#ff7f0e", alpha=0.8, linewidth=0.8)
ax2.set_ylabel("Jitter (µs)")
ax2.set_xlabel("Sequence Number")
ax1.set_title(
    "Figure 2: Inter-Arrival Jitter (Stability)", fontsize=12, fontweight="bold"
)
ax2.grid(True, linestyle="--", alpha=0.6)
ax2.set_ylim(
    0, stats_df["jitter_us"].quantile(0.99) * 2
)  

plt.tight_layout()
plt.savefig(args.output  +"/plot.png", dpi=298)
