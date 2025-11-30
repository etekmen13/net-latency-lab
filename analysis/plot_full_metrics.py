import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from latency_utils import load_binary_file, remove_clock_drift

sns.set_theme(style="whitegrid", context="paper", font_scale=1.2)


def plot_cdf(file_map, output_dir):
    plt.figure(figsize=(10, 6))

    combined_df = []

    print("Loading raw data for CDF...")
    for label, filepath in file_map.items():
        if not os.path.exists(filepath):
            print(f"Warning: {filepath} not found.")
            continue

        df = load_binary_file(filepath)
        df, _ = remove_clock_drift(df)

        temp = pd.DataFrame(
            {"latency_us": df["latency_corrected_us"], "Variant": label}
        )
        combined_df.append(temp)

    if not combined_df:
        print("No data loaded.")
        return

    full_data = pd.concat(combined_df)

    ax = sns.ecdfplot(data=full_data, x="latency_us", hue="Variant", linewidth=2)

    ax.set_title("CDF: Tail Latency Distribution (High Load)", fontweight="bold")
    ax.set_xlabel("Latency (µs)")
    ax.set_ylabel("Cumulative Probability")

    p99 = full_data["latency_us"].quantile(0.99)
    ax.set_xlim(0, p99 * 1.5)

    plt.grid(True, which="minor", linestyle=":", linewidth=0.5)
    plt.tight_layout()

    out_path = os.path.join(output_dir, "fig_5_cdf_comparison.png")
    plt.savefig(out_path, dpi=300)
    print(f"Generated CDF: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Plot CDF comparison")
    parser.add_argument("--baseline", required=True, help="Path to Baseline .bin file")
    parser.add_argument("--threaded", required=True, help="Path to Threaded .bin file")
    parser.add_argument("--out", default=".", help="Output directory")

    args = parser.parse_args()

    files = {"Baseline": args.baseline, "Threaded": args.threaded}

    plot_cdf(files, args.out)
