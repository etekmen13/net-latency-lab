"""Create formula-level resume-claim evidence from completed measurements."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from profile_tools import select_representatives


def generate(measurement_session: Path, profile_session: Path, output: Path) -> Path:
    runs = pd.read_csv(measurement_session / "per_run_summary.csv")
    profiles = pd.read_csv(profile_session / "profile_summary.csv")
    selected = {row["receiver"]: row for row in select_representatives(runs)}
    baseline = selected["baseline"]
    profile_medians = profiles.groupby("receiver", as_index=True).median(numeric_only=True)
    rows = []
    for receiver in ("batched", "threaded"):
        candidate = selected[receiver]
        improvement = float(candidate["processed_pps_median"]) / float(baseline["processed_pps_median"])
        base_profile, candidate_profile = profile_medians.loc["baseline"], profile_medians.loc[receiver]
        syscall_supported = pd.notna(base_profile.get("receive_syscalls_per_packet")) and pd.notna(candidate_profile.get("receive_syscalls_per_packet"))
        mechanism = bool(syscall_supported and
                         candidate_profile.receive_syscalls_per_packet < base_profile.receive_syscalls_per_packet and
                         candidate_profile.cycles_per_packet < base_profile.cycles_per_packet)
        passes = bool(improvement > 1.0 and mechanism)
        claim = (f"On two Raspberry Pi 4s over direct 1 GbE, {receiver} sustained "
                 f"{improvement:.2f}x the processed UDP packet rate of recvfrom at <=0.1% "
                 "application loss across all five repetitions; perf attributed the result "
                 "to fewer receive syscalls and cycles per packet.") if passes else ""
        rows.append({
            "candidate": receiver,
            "baseline_configuration": f"baseline/batch={int(baseline['batch_size'])}",
            "candidate_configuration": f"{receiver}/batch={int(candidate['batch_size'])}",
            "baseline_run_ids": baseline["run_ids"], "candidate_run_ids": candidate["run_ids"],
            "baseline_processed_pps_median": baseline["processed_pps_median"],
            "candidate_processed_pps_median": candidate["processed_pps_median"],
            "formula": "candidate_processed_pps_median / baseline_processed_pps_median",
            "improvement_ratio": improvement,
            "baseline_receive_syscalls_per_packet": base_profile.get("receive_syscalls_per_packet"),
            "candidate_receive_syscalls_per_packet": candidate_profile.get("receive_syscalls_per_packet"),
            "baseline_cycles_per_packet": base_profile.get("cycles_per_packet"),
            "candidate_cycles_per_packet": candidate_profile.get("cycles_per_packet"),
            "profile_mechanism_passes": mechanism, "claim_passes": passes,
            "resume_claim": claim,
        })
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output, index=False)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurement_session", type=Path)
    parser.add_argument("profile_session", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(generate(args.measurement_session, args.profile_session, args.output))
