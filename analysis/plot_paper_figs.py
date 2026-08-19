"""Generate the two predeclared recruiter-facing figures from physical data."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def label(row: pd.Series) -> str:
    return f"{row.receiver} b={int(row.batch_size)}"


def campaign_title(campaign: str) -> str:
    """Human title for a campaign name, e.g. work_5us -> "5 µs work"."""
    if campaign == "raw":
        return "Raw (0 ns work)"
    if campaign.startswith("work_") and campaign.endswith("us"):
        return f"{campaign[len('work_'):-len('us')]} µs work"
    if campaign == "workload_10us":
        return "10 µs work"
    return campaign


def figure_throughput(per_run: pd.DataFrame, output: Path,
                      campaigns: Sequence[str] = ("raw", "workload_10us")) -> Path:
    physical = per_run[per_run.topology != "local_loopback"]
    panels = [(name, campaign_title(name)) for name in campaigns]
    fig, axes = plt.subplots(len(panels), 2, figsize=(12, 4 * len(panels)),
                             sharex=False, squeeze=False)
    for row_index, (campaign, title) in enumerate(panels):
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


# A pipelined receiver is given a second core, so absolute capacity alone flatters
# it. Every figure that reports capacity also reports capacity per allocated core.
ALLOCATED_CORES = {"baseline": 1, "batched": 1, "threaded": 2}
RECEIVER_ORDER = ("baseline", "batched", "threaded")


def zero_loss_knees(per_run: pd.DataFrame, campaign: str) -> pd.DataFrame:
    """Highest offered rate each receiver sustained with zero application loss.

    Uses the strict zero_loss_run column rather than the campaign's 0.1% rule: a
    figure captioned "zero loss" should mean zero, not 0.1% (which is 4,500
    packets on a 30 s run at 150 kpps).
    """
    data = per_run[(per_run.campaign == campaign) &
                   (per_run.topology != "local_loopback")]
    if "run_valid" in data.columns:
        data = data[data.run_valid.astype(bool)]
    grouped = data.groupby(["receiver", "batch_size", "requested_rate_pps"], as_index=False).agg(
        repetitions=("run_id", "size"), all_zero_loss=("zero_loss_run", "all"))
    passing = grouped[grouped.all_zero_loss]
    if passing.empty:
        raise ValueError(f"No configuration in campaign {campaign!r} sustained zero loss")
    return passing.loc[passing.groupby(["receiver", "batch_size"]).requested_rate_pps.idxmax()]


def figure_pareto(throughput_runs: pd.DataFrame, latency_runs: pd.DataFrame,
                  output: Path, throughput_campaign: str = "work_5us",
                  latency_campaign: str = "latency_5us",
                  common_rate_pps: int | None = None) -> Path:
    """Capacity against tail queueing delay, with the per-core view beside it.

    The latency axis is application_queue_delay: processing_start_ts - rx_ts, both
    taken on the receiver host, so it is a single subtraction with no cross-host
    synchronisation in it. Cross-host latency is deliberately not plotted here.
    """
    knees = zero_loss_knees(throughput_runs, throughput_campaign).set_index("receiver")
    latency = latency_runs[(latency_runs.campaign == latency_campaign) &
                           (latency_runs.topology != "local_loopback")]
    if common_rate_pps is None:
        common_rate_pps = int(latency.requested_rate_pps.min())
    matched = latency[latency.requested_rate_pps == common_rate_pps]
    if matched.empty:
        raise ValueError(f"No latency runs at the common rate {common_rate_pps}")
    tails = matched.groupby("receiver").agg(
        p50=("queue_delay_p50_us", "median"), p99=("queue_delay_p99_us", "median"),
        p999=("queue_delay_p999_us", "median"), samples=("latency_sample_count", "sum"))

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for receiver in RECEIVER_ORDER:
        if receiver not in knees.index or receiver not in tails.index:
            continue
        knee = float(knees.loc[receiver, "requested_rate_pps"])
        cores = ALLOCATED_CORES[receiver]
        row = tails.loc[receiver]
        for axis, x, xlabel in ((axes[0], knee, "Max zero-loss rate (packets/s)"),
                                (axes[1], knee / cores, "Max zero-loss rate per allocated core")):
            axis.errorbar(x, row.p999,
                          yerr=[[max(row.p999 - row.p50, 0)], [0]],
                          fmt="o", markersize=9, capsize=4)
            axis.annotate(f"{receiver} (b={int(knees.loc[receiver,'batch_size'])}, "
                          f"{cores} core{'s' if cores > 1 else ''})",
                          (x, row.p999), textcoords="offset points",
                          xytext=(8, 6), fontsize=9)
            axis.set_xlabel(xlabel)
    for axis in axes:
        axis.set_yscale("log")
        axis.set_ylabel("p99.9 application queueing delay (µs)\nlower bar = p50")
        axis.grid(True, alpha=.3, which="both")
    axes[0].set_title(f"Capacity vs tail queueing delay, measured at {common_rate_pps:,} pps")
    axes[1].set_title("Same, per allocated core")
    fig.suptitle("Higher and lower is better. The pipelined receiver buys capacity "
                 "and loss observability with a second core and a longer tail.",
                 fontsize=9, y=.02)
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
