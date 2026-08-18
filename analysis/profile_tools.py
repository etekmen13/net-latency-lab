"""Mechanically select profile configurations and summarize perf evidence."""

from __future__ import annotations

import argparse
import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

DEFAULT_EVENTS = [
    "task-clock", "cycles", "instructions", "branches", "branch-misses",
    "cache-references", "cache-misses", "context-switches", "cpu-migrations",
    "page-faults", "syscalls:sys_enter_recvfrom", "syscalls:sys_enter_recvmmsg",
]


REQUIRED_REPETITIONS = 5
MAX_APPLICATION_LOSS_PCT = 0.1
# Extra columns that must match within a repetition group.  Without them two
# different workloads (for example a sampled and an unsampled run) that happen
# to share receiver/batch/rate could be merged into one "five repetition" group.
CONFIGURATION_KEYS = ("mode", "burst_size", "payload_size", "duration_seconds",
                      "work_ns", "sample_every")


def claim_population(per_run: pd.DataFrame, campaign: str = "raw") -> pd.DataFrame:
    """Rows eligible for a claim: right campaign, physical topology, valid run.

    ``topology`` is mandatory.  Loopback numbers must never reach the claim
    gate, and silently skipping the filter when the column is absent would be
    exactly the kind of quiet failure this gate exists to prevent.
    """
    if "topology" not in per_run.columns:
        raise ValueError("per-run summary is missing topology; cannot exclude loopback data")
    data = per_run[per_run.campaign == campaign]
    data = data[data.topology != "local_loopback"]
    if "run_valid" in data.columns:
        data = data[data.run_valid.astype(bool)]
    return data


def sustainable_groups(per_run: pd.DataFrame, campaign: str = "raw") -> pd.DataFrame:
    """Configurations whose full repetition set met the sustainability rule.

    Also reports ``max_tested_rate_pps``/``saturated`` so callers can tell a
    genuinely receiver-limited result from a sweep that simply stopped before
    the receiver broke.  Without that distinction a sender-limited campaign
    makes every receiver tie at the offered rate and any ratio between them is
    measurement noise.
    """
    data = claim_population(per_run, campaign)
    configuration = [key for key in CONFIGURATION_KEYS if key in data.columns]
    group_keys = ["receiver", "batch_size", "requested_rate_pps"] + configuration
    # Saturation must be judged against the highest rate tested for the SAME
    # workload. Grouping only by receiver/batch would compare a 5 us sweep that
    # tops out at 210k against a 0 ns sweep that reached 950k and mark every 5 us
    # group "saturated" on the strength of an unrelated campaign's maximum.
    highest_tested = data.groupby(["receiver", "batch_size"] + configuration
                                  ).requested_rate_pps.max()
    rows = []
    for key, group in data.groupby(group_keys):
        receiver, batch_size, rate = key[0], int(key[1]), int(key[2])
        if len(group) != REQUIRED_REPETITIONS or not bool(group.sustainable_run.all()):
            continue
        maximum_tested = int(highest_tested.loc[(receiver, batch_size, *key[3:])])
        row = {"receiver": receiver, "batch_size": batch_size,
               "requested_rate_pps": rate,
               "repetitions": len(group),
               "processed_pps_median": float(group.processed_pps.median()),
               "processed_pps_min": float(group.processed_pps.min()),
               "processed_pps_max": float(group.processed_pps.max()),
               "application_loss_pct_max": float(group.application_loss_pct.max()),
               "max_tested_rate_pps": maximum_tested,
               "saturated": bool(rate < maximum_tested),
               "campaign": campaign,
               "run_ids": "|".join(sorted(group.run_id.astype(str)))}
        # Carry the workload identity into the row. Without it downstream
        # selection cannot tell one workload from another and silently mixes
        # them; see select_representatives.
        row.update({name: group[name].iloc[0] for name in configuration})
        rows.append(row)
    return pd.DataFrame(rows)


def select_representatives(per_run: pd.DataFrame, campaign: str = "raw",
                           work_ns: int | None = None) -> list[dict[str, Any]]:
    passing = sustainable_groups(per_run, campaign)
    if passing.empty:
        raise ValueError(f"No {campaign} configuration has five sustainable repetitions")
    if work_ns is not None:
        passing = passing[passing.work_ns == int(work_ns)]
        if passing.empty:
            raise ValueError(f"No {campaign} configuration at work_ns={work_ns} "
                             "has five sustainable repetitions")
    selected = []
    for receiver in ("baseline", "batched", "threaded"):
        candidates = passing[passing.receiver == receiver]
        if candidates.empty:
            raise ValueError(f"No sustainable configuration for {receiver}")
        # Each batch competes at its highest sustainable rate, per workload.
        # Grouping by batch_size alone let the highest-rate row win outright, so
        # a 0 ns baseline could be paired against a 5 us candidate: the rate is
        # always higher without per-packet work, and nothing downstream compares
        # the two rows' work_ns.
        grouping = ["batch_size"] + [key for key in ("work_ns", "sample_every")
                                     if key in candidates.columns]
        candidates = candidates.loc[candidates.groupby(grouping).requested_rate_pps.idxmax()]
        best_pps = float(candidates.processed_pps_median.max())
        within_two_percent = candidates[candidates.processed_pps_median >= .98 * best_pps]
        choice = within_two_percent.sort_values(["batch_size", "requested_rate_pps"],
                                                ascending=[True, False]).iloc[0]
        selected.append(choice.to_dict())
    workloads = {row.get("work_ns") for row in selected}
    if len(workloads) > 1:
        raise ValueError(f"Selected representatives span multiple workloads: {sorted(workloads)}")
    return selected


def prepare_profile_config(session: Path, source_config: Path, output: Path,
                           campaign: str = "raw",
                           work_ns: int | None = None) -> Path:
    per_run = pd.read_csv(session / "per_run_summary.csv")
    selected = select_representatives(per_run, campaign, work_ns)
    profile_rate = math.floor(.8 * min(int(row["requested_rate_pps"]) for row in selected))
    source = yaml.safe_load(source_config.read_text(encoding="utf-8"))
    if not isinstance(source, dict) or not isinstance(source.get("global"), dict):
        raise ValueError(f"Source config has no global section: {source_config}")
    config = {"global": deepcopy(source["global"]), "benchmarks": []}
    config["global"].setdefault("runtime", {})["repetitions"] = 3
    config["global"]["local_data_dir"] = "results/sessions"
    for row in selected:
        receiver = str(row["receiver"])
        binary = {"baseline": "receiver_baseline", "batched": "receiver_batched",
                  "threaded": "receiver_threaded"}[receiver]
        def common(row: dict[str, Any] = row) -> dict[str, Any]:
            # Fresh objects per benchmark: sharing one dict makes yaml.safe_dump
            # emit anchors/aliases, so the published config snapshot no longer
            # reads as a literal, independently checkable benchmark list.
            #
            # The workload is taken from the selected row rather than hardcoded.
            # Profiling at work_ns=0 while claiming a result measured at 5 us
            # would make cycles_per_packet in the mechanism gate describe a
            # different experiment from the one being published.
            return {
                "receiver": {"binary": binary, "batch_sizes": [int(row["batch_size"])],
                             "work_ns": int(row.get("work_ns", 0)),
                             "sample_every": int(row.get("sample_every", 0))},
                "sender": {"mode": "steady",
                           "duration_seconds": float(row.get("duration_seconds", 30)),
                           "rates_pps": [profile_rate], "burst_sizes": [1],
                           "payload_size": int(row.get("payload_size", 64))},
            }
        config["benchmarks"].append({"name": f"profile_stat_{receiver}",
            "campaign": "profile", "repetitions": 3, "profile": "stat",
            "perf_events": list(DEFAULT_EVENTS), **common()})
        config["benchmarks"].append({"name": f"profile_record_{receiver}",
            "campaign": "profile", "repetitions": 1, "profile": "record", **common()})
    output.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    plan = {"schema_version": 1, "source_session": str(session),
            "selection_rule": "highest median processed PPS among sustainable batches; within 2%, smaller batch",
            "load_rule": "80% of the lowest selected sustainable requested rate",
            "campaign": campaign,
            "work_ns": int(selected[0].get("work_ns", 0)),
            "profile_rate_pps": profile_rate,
            "saturated": {str(row["receiver"]): bool(row.get("saturated", False))
                          for row in selected},
            "selected": selected}
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


def _require_nonempty(path: Path, description: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Missing or empty {description}: {path}")


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    _require_nonempty(path, description)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Malformed {description}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Malformed {description}: {path}: expected a JSON object")
    return value


def validate_profile_session(session: Path) -> Path:
    """Validate the complete, fixed 12-run profile evidence set."""
    if not session.is_dir():
        raise ValueError(f"Profile session is not a directory: {session}")
    manifest_path = session / "session_manifest.json"
    manifest = _read_json_object(manifest_path, "session manifest")
    runs = manifest.get("runs")
    if not isinstance(runs, list) or len(runs) != 12:
        count = len(runs) if isinstance(runs, list) else "missing"
        raise ValueError(f"Expected exactly 12 profile runs, found {count}")

    modes: list[str] = []
    run_ids: set[str] = set()
    metadata_paths: set[Path] = set()
    for index, run_entry in enumerate(runs, start=1):
        if not isinstance(run_entry, dict):
            raise ValueError(f"Run {index} is missing manifest metadata")
        metadata_name = run_entry.get("metadata")
        trace_name = run_entry.get("trace")
        if not isinstance(metadata_name, str) or not metadata_name:
            raise ValueError(f"Run {index} is missing manifest metadata")
        if not isinstance(trace_name, str) or not trace_name:
            raise ValueError(f"Run {index} is missing its trace path")
        metadata_path = session / metadata_name
        if metadata_path in metadata_paths:
            raise ValueError(f"Duplicate metadata path in manifest: {metadata_name}")
        metadata_paths.add(metadata_path)
        metadata = _read_json_object(metadata_path, "run metadata")

        run_id = metadata.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"Run metadata is missing run_id: {metadata_path}")
        if run_id in run_ids:
            raise ValueError(f"Duplicate run_id: {run_id}")
        run_ids.add(run_id)
        if run_entry.get("run_id") != run_id:
            raise ValueError(f"Manifest and metadata run_id disagree for {metadata_path}")
        validity = metadata.get("validity")
        if not isinstance(validity, dict) or validity.get("valid") is not True:
            raise ValueError(f"Profile run is invalid: {run_id}")
        run_description = metadata.get("run")
        if not isinstance(run_description, dict):
            raise ValueError(f"Run is missing run metadata: {run_id}")
        if run_description.get("campaign") != "profile":
            raise ValueError(f"Run is not in the profile campaign: {run_id}")

        processes = metadata.get("processes")
        if not isinstance(processes, dict):
            raise ValueError(f"Run is missing process metadata: {run_id}")
        receiver_pid = processes.get("receiver_pid")
        wrapper_pid = processes.get("profile_wrapper_pid")
        if (not isinstance(receiver_pid, int) or isinstance(receiver_pid, bool)
                or receiver_pid <= 0 or not isinstance(wrapper_pid, int)
                or isinstance(wrapper_pid, bool) or wrapper_pid <= 0):
            raise ValueError(f"Run has invalid receiver/profile-wrapper PIDs: {run_id}")
        if receiver_pid == wrapper_pid:
            raise ValueError(f"Receiver and profile-wrapper PIDs alias in {run_id}")

        trace_path = session / trace_name
        _require_nonempty(trace_path, f"trace for {run_id}")
        if not metadata_path.name.endswith("_meta.json"):
            raise ValueError(f"Unexpected metadata filename: {metadata_path}")
        stats_path = metadata_path.with_name(
            metadata_path.name.removesuffix("_meta.json") + "_rx_stats.json")
        _read_json_object(stats_path, f"receiver stats for {run_id}")
        if not isinstance(metadata.get("receiver_stats"), dict):
            raise ValueError(f"Run metadata is missing receiver stats: {run_id}")

        profile = metadata.get("profile")
        if not isinstance(profile, dict):
            raise ValueError(f"Run is missing profile metadata: {run_id}")
        mode = profile.get("mode")
        if mode not in {"stat", "record"}:
            raise ValueError(f"Unexpected profile mode for {run_id}: {mode!r}")
        modes.append(mode)
        if mode == "stat":
            artifact = profile.get("artifact")
            if not isinstance(artifact, str) or not artifact:
                raise ValueError(f"Stat run is missing its perf artifact: {run_id}")
            _require_nonempty(metadata_path.parent / artifact,
                              f"perf stat artifact for {run_id}")
        else:
            perf_data, report = profile.get("perf_data"), profile.get("report")
            if not isinstance(perf_data, str) or not perf_data:
                raise ValueError(f"Record run is missing its perf artifact: {run_id}")
            if not isinstance(report, str) or not report:
                raise ValueError(f"Record run is missing its perf report: {run_id}")
            _require_nonempty(metadata_path.parent / perf_data,
                              f"perf record artifact for {run_id}")
            _require_nonempty(metadata_path.parent / report,
                              f"perf record report for {run_id}")

    if modes.count("stat") != 9 or modes.count("record") != 3:
        raise ValueError(
            f"Expected 9 stat and 3 record runs, found "
            f"{modes.count('stat')} stat and {modes.count('record')} record")
    return session.resolve()


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
        unavailable = sorted(event for event in DEFAULT_EVENTS
                             if event in counters and counters[event] is None)
        missing = sorted(event for event in DEFAULT_EVENTS if event not in counters)
        receive_events = ("syscalls:sys_enter_recvfrom", "syscalls:sys_enter_recvmmsg")
        receive_values = [counters.get(name) for name in receive_events]
        # An unavailable counter is unknown, not zero.  Summing it as zero
        # understates syscalls per packet and would fabricate support for the
        # "fewer receive syscalls" mechanism.
        recv_syscalls = (None if any(value is None for value in receive_values)
                         else sum(receive_values))
        def per_packet(event: str) -> float | None:
            value = counters.get(event)
            return value / processed if value is not None and processed else None
        cycles, instructions = counters.get("cycles"), counters.get("instructions")
        context_switches = counters.get("context-switches")
        rows.append({
            "run_id": metadata["run_id"], "receiver": metadata["run"]["receiver_variant"],
            "batch_size": metadata["run"]["batch_size"], "processed_packets": processed,
            "cycles_per_packet": per_packet("cycles"),
            "instructions_per_packet": per_packet("instructions"),
            "cache_misses_per_packet": per_packet("cache-misses"),
            "receive_syscalls_per_packet": (recv_syscalls / processed
                                            if recv_syscalls is not None and processed else None),
            "context_switches_per_second": (context_switches / elapsed
                                            if context_switches is not None and elapsed else None),
            "context_switches_per_packet": per_packet("context-switches"),
            "ipc": instructions / cycles if instructions is not None and cycles else None,
            "unavailable_events": "; ".join(unavailable),
            "absent_events": "; ".join(missing),
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
    prepare.add_argument("--campaign", default="raw",
                         help="campaign whose representatives are profiled")
    prepare.add_argument("--work-ns", type=int, default=None,
                         help="restrict selection to this per-packet work budget")
    summarize = sub.add_parser("summarize")
    summarize.add_argument("session", type=Path)
    validate = sub.add_parser("validate")
    validate.add_argument("session", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare_profile_config(args.session, args.config, args.output,
                                     args.campaign, args.work_ns))
    elif args.command == "summarize":
        print(summarize_profiles(args.session))
    else:
        print(validate_profile_session(args.session))
