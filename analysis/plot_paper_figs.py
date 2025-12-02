import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from latency_utils import load_binary_file, remove_clock_drift

sns.set_theme(style="whitegrid", context="paper", font_scale=1.2)


def get_variant_label(row):
    """Helper to create distinct labels for plots"""
    if "threaded" in row["receiver"]:
        b = int(row.get("batch_size", 1))
        return f"Threaded (B={b})"
    return "Baseline"


def plot_steady_state_curve(df, output_dir):
    """Fig 1: Latency vs PPS (Steady Mode only)"""
    subset = df[(df["mode"] == "steady") & (df["burst_size"] <= 1)].copy()
    if subset.empty:
        return

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

    ax.set_title("Steady State: Latency vs. Throughput", fontweight="bold")
    ax.set_xlabel("Offered Load (Packets/Sec)")
    ax.set_ylabel("99th Percentile Latency (µs)")
    ax.set_yscale("log")
    ax.grid(True, which="minor", linestyle=":", linewidth=0.5)

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "fig_1_steady_latency.png"), dpi=300)
    print("Generated Fig 1: Steady State Curve")


def plot_burst_impact(df, output_dir):
    """Fig 2: Burst Tolerance - Latency vs Burst Size"""
    subset = df[df["mode"] == "burst"].copy()
    if subset.empty:
        return

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


def plot_jitter_and_loss(df, output_dir):
    """
    Fig 3 (Revised): Jitter Stability vs. Load with Loss Overlay
    Uses a Dual Y-Axis to show that low jitter at high load
    is a symptom of packet loss (Survivor Bias).
    """
    subset = df[(df["mode"] == "steady") & (df["burst_size"] <= 1)].copy()
    if subset.empty:
        return

    subset["Variant"] = subset.apply(get_variant_label, axis=1)

    # Setup the figure and primary axis (Jitter)
    fig, ax1 = plt.subplots(figsize=(10, 6))

    # Plot 1: Jitter on Left Axis (Solid Lines)
    sns.lineplot(
        data=subset,
        x="rate_pps",
        y="jitter_p99",
        hue="Variant",
        style="Variant",
        markers=True,
        dashes=False,  # Force solid lines for Jitter
        linewidth=2.5,
        markersize=9,
        ax=ax1,
        legend=False,  # We will build a custom legend
    )

    ax1.set_title("Jitter Stability vs. Load (with Packet Loss)", fontweight="bold")
    ax1.set_xlabel("Offered Load (Packets/Sec)")
    ax1.set_ylabel("99th %ile Jitter (µs)")
    ax1.grid(True, which="minor", linestyle=":", linewidth=0.5)

    # Setup the secondary axis (Packet Loss)
    ax2 = ax1.twinx()

    # Plot 2: Packet Loss on Right Axis (Dashed Lines)
    # We use the same hue order so colors match the Jitter lines
    variants = sorted(subset["Variant"].unique())
    palette = sns.color_palette(n_colors=len(variants))

    for i, variant in enumerate(variants):
        var_data = subset[subset["Variant"] == variant]
        ax2.plot(
            var_data["rate_pps"],
            var_data["loss_pct"],
            color=palette[i],
            linestyle="--",  # Dashed for Loss
            alpha=0.6,  # Slightly transparent
            linewidth=2,
        )

    ax2.set_ylabel("Packet Loss (%) - (Dashed Lines)")
    ax2.set_ylim(0, 100)  # Fix loss scale 0-100%

    # Custom Legend
    from matplotlib.lines import Line2D

    legend_elements = []
    for i, variant in enumerate(variants):
        # Add the color entry for the variant
        legend_elements.append(Line2D([0], [0], color=palette[i], lw=2, label=variant))

    # Add key for Solid vs Dashed
    legend_elements.append(Line2D([0], [0], color="gray", lw=2, label="Solid = Jitter"))
    legend_elements.append(
        Line2D([0], [0], color="gray", lw=2, linestyle="--", label="Dashed = Loss")
    )

    ax1.legend(handles=legend_elements, loc="upper left")

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "fig_3_jitter_with_loss.png"), dpi=300)
    print("Generated Fig 3: Jitter Curve with Loss Overlay")


def plot_packet_loss(df, output_dir):
    """Fig 4: Packet Loss vs Throughput (The 'Cliff')"""
    subset = df[(df["mode"] == "steady") & (df["burst_size"] <= 1)].copy()
    if subset.empty:
        return

    subset["Variant"] = subset.apply(get_variant_label, axis=1)

    plt.figure(figsize=(10, 6))
    ax = sns.lineplot(
        data=subset,
        x="rate_pps",
        y="loss_pct",
        hue="Variant",
        style="Variant",
        markers=True,
        linewidth=2.5,
    )

    ax.set_title("Reliability: Packet Loss vs. Load", fontweight="bold")
    ax.set_xlabel("Offered Load (Packets/Sec)")
    ax.set_ylabel("Packet Loss (%)")

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "fig_4_packet_loss.png"), dpi=300)
    print("Generated Fig 4: Packet Loss")


def plot_cdf_comparison(df, output_dir):
    """
    Fig 5: Automatic Deep Dive CDF
    Finds the highest rate shared by Baseline and Threaded(B=32)
    and plots their tail latency distribution.
    """
    steady = df[df["mode"] == "steady"]

    base_rates = set(steady[steady["receiver"] == "baseline"]["rate_pps"])

    threaded_subset = steady[steady["receiver"].str.contains("threaded")]
    if threaded_subset.empty:
        return

    champion_batch = 32
    if champion_batch not in threaded_subset["batch_size"].values:
        champion_batch = threaded_subset["batch_size"].max()

    thread_rates = set(
        threaded_subset[threaded_subset["batch_size"] == champion_batch]["rate_pps"]
    )

    common = sorted(list(base_rates.intersection(thread_rates)))
    if not common:
        print("No common rate found for CDF comparison.")
        return

    target_rate = common[-1]
    print(
        f"Generating CDF for Rate: {target_rate} PPS (Comparing Baseline vs Threaded B={champion_batch})"
    )

    row_base = steady[
        (steady["receiver"] == "baseline") & (steady["rate_pps"] == target_rate)
    ].iloc[0]
    row_thread = steady[
        (steady["receiver"].str.contains("threaded"))
        & (steady["batch_size"] == champion_batch)
        & (steady["rate_pps"] == target_rate)
    ].iloc[0]

    def get_path(row):
        fname = f"{row['campaign']}_{row['mode']}_{row['rate_pps']}pps_b{row['burst_size']}_batch{row['batch_size']}.bin"
        return os.path.join(output_dir, row["campaign"], fname)

    files_to_load = {
        "Baseline": get_path(row_base),
        f"Threaded (B={champion_batch})": get_path(row_thread),
    }

    combined_df = []
    for label, filepath in files_to_load.items():
        if not os.path.exists(filepath):
            print(f"  Missing binary: {filepath}")
            continue

        raw_df = load_binary_file(filepath)
        if raw_df.empty:
            continue

        clean_df, _ = remove_clock_drift(raw_df)

        temp = pd.DataFrame(
            {"latency_us": clean_df["latency_corrected_us"], "Variant": label}
        )
        combined_df.append(temp)

    if not combined_df:
        return

    full_data = pd.concat(combined_df)

    plt.figure(figsize=(10, 6))
    ax = sns.ecdfplot(data=full_data, x="latency_us", hue="Variant", linewidth=2)

    ax.set_title(f"CDF: Tail Latency @ {target_rate} PPS", fontweight="bold")
    ax.set_xlabel("Latency (µs)")
    ax.set_ylabel("Cumulative Probability")

    limit = full_data["latency_us"].quantile(0.999)
    ax.set_xlim(0, limit * 1.2)

    plt.grid(True, which="minor", linestyle=":", linewidth=0.5)
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "fig_5_cdf_comparison.png"), dpi=300)
    print("Generated Fig 5: CDF Comparison")


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
    plot_jitter_and_loss(df, output_dir)
    plot_packet_loss(df, output_dir)
    plot_cdf_comparison(df, output_dir)


if __name__ == "__main__":
    main()
