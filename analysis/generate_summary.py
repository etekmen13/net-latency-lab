import os
import yaml
import glob
import pandas as pd
import numpy as np
import argparse
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from latency_utils import load_binary_file, remove_clock_drift


def process_directory(root_dir):
    summary_rows = []

    meta_files = glob.glob(os.path.join(root_dir, "**", "*_meta.yaml"), recursive=True)
    print(f"Found {len(meta_files)} experiments.")

    for meta_path in meta_files:
        exp_dir = os.path.dirname(meta_path)
        base_name = os.path.basename(meta_path).replace("_meta.yaml", "")
        bin_path = os.path.join(exp_dir, f"{base_name}.bin")

        with open(meta_path, "r") as f:
            config = yaml.safe_load(f)

        if not os.path.exists(bin_path):
            continue

        df = load_binary_file(bin_path)
        if df.empty:
            continue

        seq_min, seq_max = df["seq"].min(), df["seq"].max()
        expected = seq_max - seq_min + 1
        loss_pct = 100.0 * (1.0 - (len(df) / expected)) if expected > 0 else 0

        df_clean, drift_slope = remove_clock_drift(df)
        lat = df_clean["latency_corrected_us"]

        rx_bin = config.get("rx_binary", "unknown").replace("receiver_", "")
        batch_size = config.get("batch_size", 1) if "threaded" in rx_bin else 1
        if "threaded" in rx_bin:
            display_name = f"{rx_bin} (B={batch_size})"
        else:
            display_name = rx_bin
        row = {
            "campaign": config.get("campaign", "unknown"),
            "receiver": display_name,
            "rate_pps": int(config.get("rate_pps", 0)),
            "mode": config.get("mode", "steady"),
            "burst_size": int(config.get("burst", 1)),
            "batch_size": int(batch_size),
            "duration": config.get("duration", 0),
            "throughput_rx_pps": len(df) / config.get("duration", 1),
            "loss_pct": loss_pct,
            "lat_min": lat.min(),
            "lat_mean": lat.mean(),
            "lat_p50": lat.median(),
            "lat_p99": lat.quantile(0.99),
            "lat_p999": lat.quantile(0.999),
            "lat_max": lat.max(),
            "lat_std": lat.std(),
            "jitter_p99": df_clean["jitter_us"].quantile(0.99),
        }
        summary_rows.append(row)

    if summary_rows:
        master_df = pd.DataFrame(summary_rows)
        master_df.sort_values(by=["receiver", "rate_pps", "burst_size"], inplace=True)

        out_file = os.path.join(root_dir, "master_summary.csv")
        master_df.to_csv(out_file, index=False)
        print(f"Summary generated: {out_file}")
    else:
        print("No valid data found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dir", help="Root directory containing session data")
    args = parser.parse_args()
    process_directory(args.dir)
