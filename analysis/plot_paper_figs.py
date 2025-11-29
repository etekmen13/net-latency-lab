import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import argparse
import os

sns.set_theme(style="whitegrid", context="paper", font_scale=1.2)


def plot_steady_state_curve(df, output_dir):
    """
    Fig 1: Latency vs PPS (Steady Mode only)
    """
    subset = df[(df["mode"] == "steady") & (df["burst_size"] <= 1)].copy()

    if subset.empty:
        print("Skipping Steady State Plot (No steady data found)")
        return

    def get_variant_label(row):
        if "threaded" in row["receiver"]:
            b = int(row.get("batch_size", 1))
            return f"Threaded (B={b})"
        return "Baseline"

    subset["Variant"] = subset.apply(get_variant_label, axis=1)

    plt.figure(figsize=(10, 6))

    ax = sns.lineplot(
        data=subset,
        x="rate_pps",
        y="lat_p99",
        hue="Variant",
        style="Variant",
        markers=True,
        dashes=False,
        linewidth=2.5,
        markersize=9,
    )

    ax.set_title(
        "Steady State: Latency vs. Throughput (Linear Scale)", fontweight="bold"
    )
    ax.set_xlabel("Offered Load (Packets/Sec)")
    ax.set_ylabel("99th Percentile Latency (µs)")

    ax.set_yscale("log")

    ax.grid(True, which="minor", linestyle=":", linewidth=0.5)

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "fig_1_steady_latency.png"), dpi=300)
    print("Generated Fig 1: Steady State Curve")


def plot_burst_impact(df, output_dir):
    """
    Fig 2: Burst Tolerance - Latency vs Burst Size
    """
    subset = df[df["mode"] == "burst"].copy()

    if subset.empty:
        print("Skipping Burst Plot (No burst data found)")
        return

    def get_variant_label(row):
        if "threaded" in row["receiver"]:
            b = int(row.get("batch_size", 1))
            return f"Threaded (B={b})"
        return "Baseline"

    subset["Variant"] = subset.apply(get_variant_label, axis=1)

    plt.figure(figsize=(8, 5))

    ax = sns.barplot(
        data=subset, x="burst_size", y="lat_p99", hue="Variant", palette="viridis"
    )

    ax.set_title("Burst Resilience", fontweight="bold")
    ax.set_xlabel("Burst Size (Packets)")
    ax.set_ylabel("99th %ile Latency (µs)")

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "fig_2_burst_impact.png"), dpi=300)
    print("Generated Fig 2: Burst Impact")


def plot_jitter_comparison(df, output_dir):
    """
    Fig 3: Jitter Stability.
    """
    plt.figure(figsize=(8, 5))

    max_indices = df.groupby(["receiver", "batch_size"])["rate_pps"].idxmax()
    subset = df.loc[max_indices].copy()

    def get_variant_label(row):
        if "threaded" in row["receiver"]:
            b = int(row.get("batch_size", 1))
            return f"Threaded (B={b})"
        return "Baseline"

    subset["Variant"] = subset.apply(get_variant_label, axis=1)

    ax = sns.barplot(
        data=subset, x="Variant", y="jitter_p99", hue="rate_pps", palette="magma"
    )

    ax.set_title("Peak Load Jitter Stability", fontweight="bold")
    ax.set_ylabel("99th %ile Jitter (µs)")
    ax.set_xlabel("Implementation")

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "fig_3_jitter_bar.png"), dpi=300)
    print("Generated Fig 3: Jitter Bar")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("summary_csv", help="Path to master_summary.csv")
    args = parser.parse_args()

    if not os.path.exists(args.summary_csv):
        print("Summary CSV not found.")
        return

    df = pd.read_csv(args.summary_csv)
    output_dir = os.path.dirname(args.summary_csv)

    print(f"Loaded {len(df)} runs. Generating figures...")

    plot_steady_state_curve(df, output_dir)
    plot_burst_impact(df, output_dir)
    plot_jitter_comparison(df, output_dir)


if __name__ == "__main__":
    main()
