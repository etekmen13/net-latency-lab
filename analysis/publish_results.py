"""Validate and export a completed physical session without inventing claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from generate_claim_evidence import generate as generate_claims
from plot_paper_figs import figure_pareto, figure_profiles, figure_throughput
from profile_tools import validate_profile_session


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish(measurement: Path, profile: Path, output: Path,
            campaigns: Sequence[str] = ("raw", "workload_10us"),
            claim_campaign: str | None = None,
            claim_work_ns: int | None = None,
            latency_session: Path | None = None,
            latency_campaign: str = "latency_5us") -> Path:
    """Export a completed session. `latency_session` is a SEPARATE session: its
    runs use fewer repetitions than the five-per-tuple comparative rule, so it
    must not be folded into `campaigns`."""
    campaigns = list(campaigns)
    claim_campaign = claim_campaign or campaigns[0]
    if claim_campaign not in campaigns:
        raise ValueError(f"Claim campaign {claim_campaign!r} is not among the published "
                         f"campaigns {campaigns}")
    runs = pd.read_csv(measurement / "per_run_summary.csv")
    physical = runs[runs.topology != "local_loopback"]
    if physical.empty:
        raise ValueError("Publication requires physical Ethernet data")
    for campaign in campaigns:
        if physical[physical.campaign == campaign].empty:
            raise ValueError(f"Physical campaign {campaign!r} has no runs")
    comparison = physical[physical.campaign.isin(campaigns)]
    grouped = comparison.groupby(["campaign", "receiver", "batch_size", "requested_rate_pps"])
    bad_repetitions = [str(key) for key, group in grouped if len(group) != 5]
    if bad_repetitions:
        raise ValueError("Every comparative tuple must have five repetitions: " + ", ".join(bad_repetitions))
    validate_profile_session(profile)
    profiles = pd.read_csv(profile / "profile_summary.csv")
    if set(profiles.receiver) != {"baseline", "batched", "threaded"}:
        raise ValueError("Profiles must cover baseline, batched, and threaded receivers")
    profile_counts = profiles.groupby("receiver").size()
    if any(profile_counts.get(receiver, 0) != 3 for receiver in ("baseline", "batched", "threaded")):
        raise ValueError("Profiles require exactly three perf stat repetitions per receiver")
    report_receivers = {path.name.split("profile_record_", 1)[1].split("_r", 1)[0]
                        for path in profile.glob("**/*_perf_report.txt")
                        if "profile_record_" in path.name}
    if report_receivers != {"baseline", "batched", "threaded"}:
        raise ValueError("One perf report extract is required per receiver")
    output.mkdir(parents=True, exist_ok=True)
    cleaned = pd.read_csv(measurement / "repetition_summary.csv")
    cleaned.to_csv(output / "benchmark_summary.csv", index=False)
    generate_claims(measurement, profile, output / "claim_evidence.csv",
                    claim_campaign, claim_work_ns)
    figure_throughput(runs, output / "figure1_throughput_loss.png", campaigns)
    figure_profiles(profiles, output / "figure2_profile_mechanism.png")
    if latency_session is not None:
        latency_runs = pd.read_csv(latency_session / "per_run_summary.csv")
        figure_pareto(runs, latency_runs, output / "figure3_capacity_vs_tail_latency.png",
                      claim_campaign, latency_campaign)
        (output / "latency_summary.csv").write_bytes(
            (latency_session / "repetition_summary.csv").read_bytes())
    for report in profile.glob("**/*_perf_report.txt"):
        shutil.copy2(report, output / report.name)
    measurement_manifest = json.loads((measurement / "session_manifest.json").read_text())
    profile_manifest = json.loads((profile / "session_manifest.json").read_text())
    environment = {"measurement": measurement_manifest["environment"],
                   "profile": profile_manifest["environment"],
                   "benchmark_commit": measurement_manifest.get("benchmark_commit")}
    (output / "environment.json").write_text(json.dumps(environment, indent=2) + "\n")
    artifacts = sorted({*measurement.rglob("*"), *profile.rglob("*")})
    checksums = [{"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}
                 for path in artifacts if path.is_file()]
    (output / "checksum_manifest.json").write_text(json.dumps(checksums, indent=2) + "\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurement_session", type=Path)
    parser.add_argument("profile_session", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--campaigns", nargs="+", default=["raw", "workload_10us"],
                        help="comparative campaigns to publish")
    parser.add_argument("--claim-campaign", default=None,
                        help="campaign the resume claim is drawn from (default: the first)")
    parser.add_argument("--claim-work-ns", type=int, default=None,
                        help="restrict the claim to this per-packet work budget")
    parser.add_argument("--latency-session", type=Path, default=None,
                        help="separate session supplying the tail-latency figure")
    parser.add_argument("--latency-campaign", default="latency_5us")
    args = parser.parse_args()
    print(publish(args.measurement_session, args.profile_session, args.output,
                  args.campaigns, args.claim_campaign, args.claim_work_ns,
                  args.latency_session, args.latency_campaign))
