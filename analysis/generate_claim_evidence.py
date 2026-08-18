"""Create formula-level resume-claim evidence from completed measurements."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

from profile_tools import (MAX_APPLICATION_LOSS_PCT, REQUIRED_REPETITIONS,
                           select_representatives)

# A published ratio is rendered with two decimals, so anything below a few
# percent rounds to "1.00x".  The repetition ranges must also not overlap: two
# configurations that both simply tracked the offered rate differ only by
# measurement noise and must not become a resume sentence.
MIN_IMPROVEMENT_RATIO = 1.05
# perf counters must show a real reduction, not a floating-point tie.
MAX_CANDIDATE_SYSCALL_FRACTION = 0.95


def _finite(value: Any) -> bool:
    return value is not None and pd.notna(value)


def _throughput_blockers(baseline: dict[str, Any], candidate: dict[str, Any],
                         improvement: float) -> list[str]:
    blockers: list[str] = []
    for name, row in (("baseline", baseline), ("candidate", candidate)):
        if int(row.get("repetitions", 0)) != REQUIRED_REPETITIONS:
            blockers.append(f"{name} does not have {REQUIRED_REPETITIONS} repetitions")
        loss = row.get("application_loss_pct_max")
        if not _finite(loss) or float(loss) > MAX_APPLICATION_LOSS_PCT:
            blockers.append(f"{name} exceeds the {MAX_APPLICATION_LOSS_PCT}% application-loss rule")
        if not bool(row.get("saturated", False)):
            blockers.append(f"{name} never lost a higher offered rate, so the sweep is "
                            "sender-limited rather than receiver-limited")
    if improvement < MIN_IMPROVEMENT_RATIO:
        blockers.append(f"improvement {improvement:.4f}x is below the "
                        f"{MIN_IMPROVEMENT_RATIO}x minimum effect size")
    if float(candidate["processed_pps_min"]) <= float(baseline["processed_pps_max"]):
        blockers.append("candidate and baseline repetition ranges overlap")
    return blockers


def _mechanism_blockers(base_profile: pd.Series, candidate_profile: pd.Series,
                        baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for name, row, selected in (("baseline", base_profile, baseline),
                                ("candidate", candidate_profile, candidate)):
        if _finite(row.get("batch_size")) and int(row["batch_size"]) != int(selected["batch_size"]):
            blockers.append(f"{name} was profiled at batch {int(row['batch_size'])} but "
                            f"measured at batch {int(selected['batch_size'])}")
    base_syscalls = base_profile.get("receive_syscalls_per_packet")
    candidate_syscalls = candidate_profile.get("receive_syscalls_per_packet")
    if not (_finite(base_syscalls) and _finite(candidate_syscalls)):
        blockers.append("receive syscalls per packet unavailable")
    elif float(candidate_syscalls) > MAX_CANDIDATE_SYSCALL_FRACTION * float(base_syscalls):
        blockers.append("receive syscalls per packet not meaningfully reduced")
    base_cycles = base_profile.get("cycles_per_packet")
    candidate_cycles = candidate_profile.get("cycles_per_packet")
    if not (_finite(base_cycles) and _finite(candidate_cycles)):
        blockers.append("cycles per packet unavailable")
    elif float(candidate_cycles) >= float(base_cycles):
        blockers.append("cycles per packet not reduced")
    return blockers


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
        blockers = _throughput_blockers(baseline, candidate, improvement)
        if "baseline" in profile_medians.index and receiver in profile_medians.index:
            base_profile = profile_medians.loc["baseline"]
            candidate_profile = profile_medians.loc[receiver]
            mechanism_blockers = _mechanism_blockers(base_profile, candidate_profile,
                                                     baseline, candidate)
        else:
            base_profile = candidate_profile = pd.Series(dtype="float64")
            mechanism_blockers = [f"profile summary has no rows for baseline and {receiver}"]
        mechanism = not mechanism_blockers
        passes = bool(not blockers and mechanism)
        claim = (f"On two Raspberry Pi 4s over direct 1 GbE, {receiver} sustained "
                 f"{improvement:.2f}x the processed UDP packet rate of recvfrom at <=0.1% "
                 "application loss across all five repetitions; perf attributed the result "
                 "to fewer receive syscalls and cycles per packet.") if passes else ""
        rows.append({
            "candidate": receiver,
            "baseline_configuration": f"baseline/batch={int(baseline['batch_size'])}",
            "candidate_configuration": f"{receiver}/batch={int(candidate['batch_size'])}",
            "baseline_run_ids": baseline["run_ids"], "candidate_run_ids": candidate["run_ids"],
            "baseline_repetitions": int(baseline.get("repetitions", 0)),
            "candidate_repetitions": int(candidate.get("repetitions", 0)),
            "baseline_application_loss_pct_max": baseline.get("application_loss_pct_max"),
            "candidate_application_loss_pct_max": candidate.get("application_loss_pct_max"),
            "baseline_saturated": bool(baseline.get("saturated", False)),
            "candidate_saturated": bool(candidate.get("saturated", False)),
            "baseline_processed_pps_median": baseline["processed_pps_median"],
            "candidate_processed_pps_median": candidate["processed_pps_median"],
            "baseline_processed_pps_max": baseline["processed_pps_max"],
            "candidate_processed_pps_min": candidate["processed_pps_min"],
            "formula": "candidate_processed_pps_median / baseline_processed_pps_median",
            "improvement_ratio": improvement,
            "minimum_improvement_ratio": MIN_IMPROVEMENT_RATIO,
            "baseline_receive_syscalls_per_packet": base_profile.get("receive_syscalls_per_packet"),
            "candidate_receive_syscalls_per_packet": candidate_profile.get("receive_syscalls_per_packet"),
            "baseline_cycles_per_packet": base_profile.get("cycles_per_packet"),
            "candidate_cycles_per_packet": candidate_profile.get("cycles_per_packet"),
            "profile_mechanism_passes": mechanism,
            "claim_passes": passes,
            "claim_blockers": "; ".join(blockers + mechanism_blockers),
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
