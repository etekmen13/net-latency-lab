"""Mechanically select profile configurations and summarize perf evidence."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

DEFAULT_EVENTS = [
    "task-clock", "cycles", "instructions", "branches", "branch-misses",
    "cache-references", "cache-misses", "context-switches", "cpu-migrations",
    "page-faults", "syscalls:sys_enter_recvfrom", "syscalls:sys_enter_recvmmsg",
]


def sustainable_groups(per_run: pd.DataFrame, campaign: str = "raw") -> pd.DataFrame:
    data = per_run[(per_run.campaign == campaign) & (per_run.run_valid == True)]  # noqa: E712
    rows = []
    for key, group in data.groupby(["receiver", "batch_size", "requested_rate_pps"]):
        if len(group) == 5 and bool(group.sustainable_run.all()):
            rows.append({"receiver": key[0], "batch_size": int(key[1]),
                         "requested_rate_pps": int(key[2]),
                         "processed_pps_median": float(group.processed_pps.median()),
                         "run_ids": "|".join(sorted(group.run_id.astype(str)))})
    return pd.DataFrame(rows)


def select_representatives(per_run: pd.DataFrame) -> list[dict[str, Any]]:
    passing = sustainable_groups(per_run)
    if passing.empty:
        raise ValueError("No raw configuration has five sustainable repetitions")
    selected = []
    for receiver in ("baseline", "batched", "threaded"):
        candidates = passing[passing.receiver == receiver]
        if candidates.empty:
            raise ValueError(f"No sustainable configuration for {receiver}")
        # Each batch competes at its highest sustainable rate.
        candidates = candidates.loc[candidates.groupby("batch_size").requested_rate_pps.idxmax()]
        best_pps = float(candidates.processed_pps_median.max())
        within_two_percent = candidates[candidates.processed_pps_median >= .98 * best_pps]
        choice = within_two_percent.sort_values(["batch_size", "requested_rate_pps"],
                                                ascending=[True, False]).iloc[0]
        selected.append(choice.to_dict())
    return selected


def prepare_profile_config(session: Path, source_config: Path,
                           output: Path) -> Path:
    per_run = pd.read_csv(session / "per_run_summary.csv")
    selected = select_representatives(per_run)
    profile_rate = math.floor(.8 * min(int(row["requested_rate_pps"]) for row in selected))
    source = yaml.safe_load(source_config.read_text(encoding="utf-8"))
    config = {"global": source["global"], "benchmarks": []}
    config["global"]["runtime"]["repetitions"] = 3
    config["global"]["local_data_dir"] = "results/sessions"
    for row in selected:
        receiver = str(row["receiver"])
        binary = {"baseline": "receiver_baseline", "batched": "receiver_batched",
                  "threaded": "receiver_threaded"}[receiver]
        common = {
            "receiver": {"binary": binary, "batch_sizes": [int(row["batch_size"])],
                         "work_ns": 0, "sample_every": 0},
            "sender": {"mode": "steady", "duration_seconds": 30,
                       "rates_pps": [profile_rate], "burst_sizes": [1],
                       "payload_size": 64},
        }
        config["benchmarks"].append({"name": f"profile_stat_{receiver}",
            "campaign": "profile", "repetitions": 3, "profile": "stat",
            "perf_events": DEFAULT_EVENTS, **common})
        config["benchmarks"].append({"name": f"profile_record_{receiver}",
            "campaign": "profile", "repetitions": 1, "profile": "record", **common})
    output.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    plan = {"schema_version": 1, "source_session": str(session),
            "selection_rule": "highest median processed PPS among sustainable batches; within 2%, smaller batch",
            "load_rule": "80% of the lowest selected sustainable requested rate",
            "profile_rate_pps": profile_rate, "selected": selected}
    output.with_suffix(".plan.json").write_text(json.dumps(plan, indent=2) + "\n",
                                                encoding="utf-8")
    return output


def parse_perf_stat(path: Path) -> dict[str, float | None]:
    counters: dict[str, float | None] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        fields = raw_line.split(",")
        if len(fields) < 3:
            continue
        value_text, event = fields[0].strip(), fields[2].strip()
        if value_text.startswith("<"):
            counters[event] = None
            continue
        try:
            counters[event] = float(value_text.replace(" ", ""))
        except ValueError:
            counters[event] = None
    return counters


def perf_running_percentages(path: Path) -> dict[str, float | None]:
    percentages: dict[str, float | None] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        fields = raw_line.split(",")
        if len(fields) < 5 or not fields[2].strip():
            continue
        try:
            percentages[fields[2].strip()] = float(fields[4].strip())
        except ValueError:
            percentages[fields[2].strip()] = None
    return percentages


def summarize_profiles(session: Path) -> Path:
    rows = []
    for metadata_path in sorted(session.glob("**/*_meta.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        profile = metadata.get("profile", {})
        if profile.get("mode") != "stat":
            continue
        counters = parse_perf_stat(metadata_path.parent / profile["artifact"])
        running = perf_running_percentages(metadata_path.parent / profile["artifact"])
        severely_multiplexed = sorted(event for event in (
            "cycles", "instructions", "cache-misses", "context-switches")
            if counters.get(event) is not None and running.get(event) is not None and running[event] < 90.0)
        if severely_multiplexed:
            raise ValueError(f"{metadata['run_id']} requires rerun due to severe multiplexing: {', '.join(severely_multiplexed)}")
        processed = int(metadata["receiver_stats"]["unique_processed_packets"])
        elapsed = float(metadata["sender_stats"]["elapsed_seconds"])
        recv_syscalls = sum(counters.get(name) or 0.0 for name in
                            ("syscalls:sys_enter_recvfrom", "syscalls:sys_enter_recvmmsg"))
        def per_packet(event: str) -> float | None:
            value = counters.get(event)
            return value / processed if value is not None and processed else None
        cycles, instructions = counters.get("cycles"), counters.get("instructions")
        rows.append({
            "run_id": metadata["run_id"], "receiver": metadata["run"]["receiver_variant"],
            "batch_size": metadata["run"]["batch_size"], "processed_packets": processed,
            "cycles_per_packet": per_packet("cycles"),
            "instructions_per_packet": per_packet("instructions"),
            "cache_misses_per_packet": per_packet("cache-misses"),
            "receive_syscalls_per_packet": recv_syscalls / processed if processed and recv_syscalls else None,
            "context_switches_per_second": (counters.get("context-switches") or 0.0) / elapsed if elapsed else None,
            "context_switches_per_packet": per_packet("context-switches"),
            "ipc": instructions / cycles if instructions is not None and cycles else None,
            **{f"event_{event}": counters.get(event) for event in DEFAULT_EVENTS},
        })
    if not rows:
        raise ValueError("No perf stat metadata found")
    path = session / "profile_summary.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("session", type=Path)
    prepare.add_argument("config", type=Path)
    prepare.add_argument("output", type=Path)
    summarize = sub.add_parser("summarize")
    summarize.add_argument("session", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare_profile_config(args.session, args.config, args.output))
    else:
        print(summarize_profiles(args.session))
