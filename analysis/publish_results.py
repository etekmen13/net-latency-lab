"""Validate and export a completed physical session without inventing claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd

from generate_claim_evidence import generate as generate_claims
from plot_paper_figs import figure_profiles, figure_throughput


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish(measurement: Path, profile: Path, output: Path) -> Path:
    runs = pd.read_csv(measurement / "per_run_summary.csv")
    physical = runs[runs.topology == "distributed_ethernet"]
    if physical.empty:
        raise ValueError("Publication requires physical distributed_ethernet data")
    comparison = physical[physical.campaign.isin(["raw", "workload_10us"])]
    grouped = comparison.groupby(["campaign", "receiver", "batch_size", "requested_rate_pps"])
    bad_repetitions = [str(key) for key, group in grouped if len(group) != 5]
    if bad_repetitions:
        raise ValueError("Every comparative tuple must have five repetitions: " + ", ".join(bad_repetitions))
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
    generate_claims(measurement, profile, output / "claim_evidence.csv")
    figure_throughput(runs, output / "figure1_throughput_loss.png")
    figure_profiles(profiles, output / "figure2_profile_mechanism.png")
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
    args = parser.parse_args()
    print(publish(args.measurement_session, args.profile_session, args.output))
