"""Generate the two predeclared recruiter-facing figures from physical data."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def label(row: pd.Series) -> str:
    return f"{row.receiver} b={int(row.batch_size)}"


def figure_throughput(per_run: pd.DataFrame, output: Path) -> Path:
    physical = per_run[per_run.topology != "local_loopback"]
    campaigns = [("raw", "Raw (0 ns work)"), ("workload_10us", "10 µs work")]
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharex=False)
    for row_index, (campaign, title) in enumerate(campaigns):
        data = physical[physical.campaign == campaign]
        if data.empty:
            raise ValueError(f"Physical campaign {campaign!r} is missing")
        grouped = data.groupby(["receiver", "batch_size", "requested_rate_pps"], as_index=False).agg(
            offered_median=("offered_pps", "median"),
            processed_median=("processed_pps", "median"),
            processed_q25=("processed_pps", lambda x: x.quantile(.25)),
            processed_q75=("processed_pps", lambda x: x.quantile(.75)),
            loss_median=("application_loss_pct", "median"),
            loss_q25=("application_loss_pct", lambda x: x.quantile(.25)),
            loss_q75=("application_loss_pct", lambda x: x.quantile(.75)))
        for (_receiver, _batch), series in grouped.groupby(["receiver", "batch_size"]):
            series = series.sort_values("offered_median")
            series_label = label(series.iloc[0])
            axes[row_index, 0].plot(series.offered_median, series.processed_median,
                                    marker="o", linewidth=1, label=series_label)
            axes[row_index, 0].fill_between(series.offered_median, series.processed_q25,
                                            series.processed_q75, alpha=.12)
            axes[row_index, 1].plot(series.offered_median, series.loss_median,
                                    marker="o", linewidth=1, label=series_label)
            axes[row_index, 1].fill_between(series.offered_median, series.loss_q25,
                                            series.loss_q75, alpha=.12)
        axes[row_index, 0].set_title(f"{title}: processed rate")
        axes[row_index, 1].set_title(f"{title}: application loss")
        axes[row_index, 0].set_ylabel("Processed packets/s (median, IQR)")
        axes[row_index, 1].set_ylabel("Application loss % (median, IQR)")
        axes[row_index, 1].axhline(.1, color="black", linestyle="--", linewidth=.8)
        for axis in axes[row_index]:
            axis.set_xlabel("Offered packets/s (actual)")
            axis.grid(alpha=.25)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="center right", fontsize=7)
    fig.tight_layout(rect=(0, 0, .86, 1))
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180)
    plt.close(fig)
    return output


def figure_profiles(profile: pd.DataFrame, output: Path) -> Path:
    metrics = [
        ("cycles_per_packet", "Cycles / packet"),
        ("instructions_per_packet", "Instructions / packet"),
        ("cache_misses_per_packet", "Cache misses / packet"),
        ("receive_syscalls_per_packet", "Receive syscalls / packet"),
        ("context_switches_per_packet", "Context switches / packet"),
    ]
    profile = profile.copy()
    profile["configuration"] = profile.apply(label, axis=1)
    order = list(dict.fromkeys(profile.configuration))
    fig, axes = plt.subplots(1, 5, figsize=(16, 4))
    for axis, (column, title) in zip(axes, metrics):
        values = [profile.loc[profile.configuration == config, column].dropna() for config in order]
        axis.boxplot(values, tick_labels=order, showfliers=True)
        axis.set_title(title)
        axis.tick_params(axis="x", rotation=45, labelsize=8)
        axis.grid(axis="y", alpha=.25)
        if any(value.empty for value in values):
            axis.text(.5, .95, "unavailable events retained as NA", transform=axis.transAxes,
                      ha="center", va="top", fontsize=7)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180)
    plt.close(fig)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("per_run_csv", type=Path)
    parser.add_argument("--profile-csv", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or args.per_run_csv.parent
    data = pd.read_csv(args.per_run_csv)
    if not (data.topology != "local_loopback").any():
        print("No recruiter-facing figures generated from loopback-only data.")
    else:
        print(figure_throughput(data, output_dir / "figure1_throughput_loss.png"))
        profile_path = args.profile_csv or args.per_run_csv.parent / "profile_summary.csv"
        if not profile_path.exists():
            raise FileNotFoundError("profile_summary.csv is required for the second figure")
        print(figure_profiles(pd.read_csv(profile_path), output_dir / "figure2_profile_mechanism.png"))
