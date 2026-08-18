"""Generate auditable per-run and repetition summaries from archived sessions."""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from latency_utils import load_binary_file, remove_clock_drift, require_corrected_latency

QUANTILE_METHOD = "linear"
REQUIRED_TOP_LEVEL = {"schema_version", "run_id", "repetition_id", "topology",
                      "run", "sender_stats", "receiver_stats", "counters", "environment"}
REQUIRED_RUN_FIELDS = {"receiver_variant", "requested_rate_pps", "duration_seconds",
                       "payload_size", "mode", "burst_size", "batch_size", "work_ns"}


def validate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise ValueError("Run metadata must be an object")
    missing = sorted(REQUIRED_TOP_LEVEL - metadata.keys())
    if missing:
        raise ValueError(f"Run metadata missing fields: {', '.join(missing)}")
    if metadata["topology"] not in {"local_loopback", "distributed_ethernet",
                                     "external_generator"}:
        raise ValueError("topology must be local_loopback, distributed_ethernet, or external_generator")
    run = metadata.get("run")
    if not isinstance(run, dict):
        raise ValueError("run metadata must be an object")
    missing_run = sorted(REQUIRED_RUN_FIELDS - run.keys())
    if missing_run:
        raise ValueError(f"Run settings missing fields: {', '.join(missing_run)}")
    sender, receiver = metadata.get("sender_stats"), metadata.get("receiver_stats")
    sender_required = {"attempted_sends", "successful_sends", "failed_sends",
                       "elapsed_seconds", "achieved_successful_send_pps"}
    receiver_required = {"datagrams_received", "valid_packets", "processed_packets",
                         "short_packets", "invalid_magic", "spsc_overflow"}
    if not isinstance(sender, dict) or sender_required - sender.keys():
        raise ValueError(f"sender_stats missing fields: {', '.join(sorted(sender_required - set(sender or {})))}")
    if not isinstance(receiver, dict) or receiver_required - receiver.keys():
        raise ValueError(f"receiver_stats missing fields: {', '.join(sorted(receiver_required - set(receiver or {})))}")
    if int(sender["attempted_sends"]) != int(sender["successful_sends"]) + int(sender["failed_sends"]):
        raise ValueError("sender attempts must equal successes plus failures")
    if int(receiver["processed_packets"]) + int(receiver["spsc_overflow"]) > int(receiver["valid_packets"]):
        raise ValueError("processed plus SPSC overflow exceeds valid received packets")
    if int(metadata.get("schema_version", 1)) >= 2:
        for field in ("unique_valid_packets", "unique_processed_packets",
                      "receive_sequence_gaps", "receive_duplicates", "receive_reordered"):
            if field not in receiver:
                raise ValueError(f"receiver_stats missing v2 field: {field}")
        if not isinstance(metadata.get("validity"), dict):
            raise ValueError("v2 metadata requires validity verdict")
        if int(receiver["processed_packets"]) + int(receiver["spsc_overflow"]) != int(receiver["valid_packets"]):
            raise ValueError("v2 processed plus SPSC overflow must equal valid received packets")
    return metadata


def linear_quantile(series: pd.Series, q: float) -> float:
    return float(series.quantile(q, interpolation=QUANTILE_METHOD))


def minimum_samples_for(q: float) -> int:
    """Smallest sample count for which ``q`` is not simply the maximum.

    With the type-7/"linear" rule the quantile sits at position ``q*(n-1)``.
    Unless at least ``1/(1-q)`` observations are present, no observation is
    expected above the quantile and the reported value degenerates to the
    sample maximum, which must not be published as "p99.9".
    """
    return int(math.ceil(1.0 / (1.0 - q)))


def tail_quantile(series: pd.Series, q: float) -> float | None:
    """Linear quantile, or ``None`` when the sample cannot support the tail."""
    if len(series) < minimum_samples_for(q):
        return None
    return linear_quantile(series, q)


def _read_metadata(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle) if path.suffix == ".json" else yaml.safe_load(handle)


def _latency_fields(frame: pd.DataFrame) -> dict[str, Any]:
    names: dict[str, Any] = {
        "latency_sample_count": 0,
        "clock_drift_slope_us_per_s": None, "clock_correction_applied": False,
        "receive_latency_min_us": None, "receive_latency_mean_us": None,
        "receive_latency_p50_us": None, "receive_latency_p99_us": None,
        "receive_latency_p999_us": None, "receive_latency_max_us": None,
        "receive_latency_std_us": None, "jitter_p99_us": None,
    }
    for prefix in ("queue_delay", "processing_time", "total_latency"):
        for quantile in ("p50", "p99", "p999"):
            names[f"{prefix}_{quantile}_us"] = None
    if frame.empty:
        return names
    clean, slope = remove_clock_drift(frame)
    require_corrected_latency(clean)
    latency = clean["latency_corrected_us"]
    names.update({
        "latency_sample_count": int(len(clean)),
        "clock_drift_slope_us_per_s": slope,
        "clock_correction_applied": bool(clean["clock_correction_applied"].iloc[0]),
        "receive_latency_min_us": float(latency.min()),
        "receive_latency_mean_us": float(latency.mean()),
        "receive_latency_p50_us": tail_quantile(latency, .5),
        "receive_latency_p99_us": tail_quantile(latency, .99),
        "receive_latency_p999_us": tail_quantile(latency, .999),
        "receive_latency_max_us": float(latency.max()),
        "receive_latency_std_us": float(latency.std()),
        "jitter_p99_us": tail_quantile(clean["jitter_us"].dropna(), .99),
    })
    for source, prefix in (("application_queue_delay_us", "queue_delay"),
                           ("processing_time_us", "processing_time"),
                           ("total_application_latency_us", "total_latency")):
        values = clean[source]
        names[f"{prefix}_p50_us"] = tail_quantile(values, .5)
        names[f"{prefix}_p99_us"] = tail_quantile(values, .99)
        names[f"{prefix}_p999_us"] = tail_quantile(values, .999)
    return names


def _window_rate(first_ns: Any, last_ns: Any, packets: int) -> float | None:
    """Packets per second over a monotonic window, or None if it is unusable."""
    if first_ns is None or last_ns is None:
        return None
    span = int(last_ns) - int(first_ns)
    if span <= 0:
        return None
    return packets * 1e9 / span


def summarize_run(metadata: dict[str, Any], frame: pd.DataFrame,
                  trace_path: Path) -> dict[str, Any]:
    validate_metadata(metadata)
    run, sender, receiver = metadata["run"], metadata["sender_stats"], metadata["receiver_stats"]
    successful = int(sender["successful_sends"])
    received = int(receiver.get("unique_valid_packets", receiver["valid_packets"]))
    processed = int(receiver.get("unique_processed_packets", receiver["processed_packets"]))
    elapsed = float(sender["elapsed_seconds"])
    ingress_loss, application_loss = successful - received, successful - processed
    row: dict[str, Any] = {
        "run_id": metadata["run_id"], "repetition_id": metadata["repetition_id"],
        "topology": metadata["topology"], "trace_path": str(trace_path),
        "campaign": run.get("campaign", "legacy"), "receiver": run["receiver_variant"],
        "requested_rate_pps": int(run["requested_rate_pps"]),
        "offered_pps": successful / elapsed if elapsed > 0 else 0.0,
        "received_pps": received / elapsed if elapsed > 0 else 0.0,
        "processed_pps": processed / elapsed if elapsed > 0 else 0.0,
        "achieved_sender_rate_pps": float(sender["achieved_successful_send_pps"]),
        "sender_runtime_seconds": elapsed,
        "send_attempts": int(sender["attempted_sends"]), "send_successes": successful,
        "send_failures": int(sender["failed_sends"]), "received_packets": received,
        "processed_packets": processed, "ingress_loss": ingress_loss,
        "application_loss": application_loss,
        "ingress_loss_pct": 100.0 * ingress_loss / successful if successful else 0.0,
        "application_loss_pct": 100.0 * application_loss / successful if successful else 0.0,
        "sequence_gap_loss": int(receiver.get("receive_sequence_gaps", 0)),
        "duplicates": int(receiver.get("receive_duplicates", 0)),
        "reordered": int(receiver.get("receive_reordered", 0)),
        "processed_sequence_gaps": int(receiver.get("processed_sequence_gaps", 0)),
        "udp_rcvbuf_errors_delta": metadata["counters"].get("udp_rcvbuf_errors", {}).get("delta"),
        "nic_rx_dropped_delta": metadata["counters"].get("nic_rx_dropped", {}).get("delta"),
        "nic_rx_errors_delta": metadata["counters"].get("nic_rx_errors", {}).get("delta"),
        "nic_rx_missed_errors_delta": metadata["counters"].get("nic_rx_missed_errors", {}).get("delta"),
        "spsc_overflow": int(receiver["spsc_overflow"]),
        "receive_syscalls": int(receiver.get("receive_syscalls", 0)),
        "sampled_packets": int(receiver.get("sampled_packets", len(frame))),
        "requested_receive_buffer_bytes": int(receiver.get("requested_socket_buffer_bytes", 0)),
        "observed_receive_buffer_bytes": int(receiver.get("observed_socket_buffer_bytes", 0)),
        "requested_send_buffer_bytes": int(sender.get("requested_socket_buffer_bytes", 0)),
        "observed_send_buffer_bytes": int(sender.get("observed_socket_buffer_bytes", 0)),
        "drain_duration_ns": int(receiver.get("drain_duration_ns", 0)),
        "queue_depth_at_shutdown": int(receiver.get("queue_depth_at_shutdown", 0)),
        "socket_pending_bytes_at_shutdown": int(receiver.get("socket_pending_bytes_at_shutdown", 0)),
        "run_valid": bool(metadata.get("validity", {}).get("valid", True)),
        "invalid_reasons": "; ".join(metadata.get("validity", {}).get("reasons", [])),
        "mode": run["mode"], "burst_size": int(run["burst_size"]),
        "batch_size": int(run["batch_size"]), "payload_size": int(run["payload_size"]),
        "duration_seconds": float(run["duration_seconds"]), "work_ns": int(run["work_ns"]),
        "sample_every": int(run.get("sample_every", 1)),
    }
    # Service rate measured over the receiver's own monotonic windows. The
    # *_pps columns above divide by the sender's elapsed time, but the receiver
    # keeps draining through the post-sender drain window, so for the threaded
    # variant up to a full queue depth of packets is credited to a period it did
    # not occur in -- 4096 packets is 0.09% of a 4.5M-packet run, against a 0.1%
    # loss gate. These columns are drain-immune and need no clock synchronisation.
    row["receive_window_pps"] = _window_rate(
        receiver.get("first_receive_mono_ns"), receiver.get("last_receive_mono_ns"), received)
    row["processing_window_pps"] = _window_rate(
        receiver.get("first_processing_mono_ns"), receiver.get("last_processing_mono_ns"), processed)
    # Direct evidence of the syscall-amortisation mechanism, rather than an
    # inference from cycles.
    syscalls = int(receiver.get("receive_syscalls", 0))
    row["packets_per_receive_syscall"] = (
        float(receiver.get("valid_packets", received)) / syscalls if syscalls else None)
    row["truncated_packets"] = int(receiver.get("truncated_packets", 0))
    row["receive_out_of_window"] = int(receiver.get("receive_out_of_window", 0))
    for name in ("nic_rx_pause", "nic_tx_pause"):
        row[f"{name}_delta"] = metadata["counters"].get(name, {}).get("delta")
    lateness = sender.get("pacing_lateness_ns") or {}
    row["pacing_lateness_p50_ns"] = lateness.get("p50")
    row["pacing_lateness_p99_ns"] = lateness.get("p99")
    # The sustainability rule allows 0.1% loss, which is 4500 packets on a 30 s
    # run at 150 kpps. Record the strictly stronger condition alongside it so a
    # "zero loss" claim can mean exactly that.
    row["zero_loss_run"] = bool(application_loss == 0 and ingress_loss == 0)
    # A run only qualifies when it actually carried traffic.  Without the
    # positive-traffic guards a zero-packet or zero-rate run scores
    # offered_pps == 0 >= 0.99 * 0 and application_loss_pct == 0, i.e. missing
    # data would silently pass the sustainability gate.
    row["sustainable_run"] = bool(
        row["run_valid"] and row["send_failures"] == 0 and
        row["requested_rate_pps"] > 0 and row["send_successes"] > 0 and
        row["processed_packets"] > 0 and row["sender_runtime_seconds"] > 0 and
        row["offered_pps"] >= .99 * row["requested_rate_pps"] and
        0 <= row["application_loss_pct"] <= .1)
    row.update(_latency_fields(frame))
    return row


def aggregate_repetitions(per_run: pd.DataFrame) -> pd.DataFrame:
    group_columns = ["topology", "campaign", "receiver", "requested_rate_pps", "mode",
                     "burst_size", "batch_size", "payload_size", "duration_seconds",
                     "work_ns", "sample_every"]
    # Compatibility with unit tests constructing older frames.
    group_columns = [column for column in group_columns if column in per_run.columns]
    numeric = [column for column in per_run.select_dtypes(include="number").columns
               if column not in group_columns and column != "repetition_id"]
    rows: list[dict[str, Any]] = []
    for key, group in per_run.groupby(group_columns, dropna=False, sort=True):
        if not isinstance(key, tuple):
            key = (key,)
        row = dict(zip(group_columns, key))
        row["repetitions"] = len(group)
        if "sustainable_run" in group:
            row["all_repetitions_sustainable"] = bool(group["sustainable_run"].all())
        if "run_valid" in group:
            row["all_repetitions_valid"] = bool(group["run_valid"].all())
        incomplete: list[str] = []
        for column in numeric:
            values = group[column].dropna()
            if len(values) != len(group):
                # Aggregating over fewer repetitions than the group contains is
                # legitimate (throughput runs carry no latency trace) but it
                # must never be invisible in the published aggregate.
                incomplete.append(f"{column}:{len(values)}/{len(group)}")
            row[f"{column}_median"] = linear_quantile(values, .5) if not values.empty else None
            row[f"{column}_q25"] = linear_quantile(values, .25) if not values.empty else None
            row[f"{column}_q75"] = linear_quantile(values, .75) if not values.empty else None
        row["metrics_with_missing_repetitions"] = "; ".join(sorted(incomplete))
        rows.append(row)
    return pd.DataFrame(rows)


def process_directory(root_dir: os.PathLike[str] | str) -> tuple[Path, Path] | None:
    root = Path(root_dir)
    metadata_paths = sorted(Path(path) for pattern in ("**/*_meta.json", "**/*_meta.yaml")
                            for path in glob.glob(str(root / pattern), recursive=True))
    rows: list[dict[str, Any]] = []
    for metadata_path in metadata_paths:
        metadata = _read_metadata(metadata_path)
        if "run_id" not in metadata:
            print(f"Skipping legacy/incomplete metadata: {metadata_path}")
            continue
        trace_path = metadata_path.with_name(metadata_path.name.replace("_meta.json", ".bin").replace("_meta.yaml", ".bin"))
        if not trace_path.exists():
            raise FileNotFoundError(f"Trace referenced by metadata is missing: {trace_path}")
        rows.append(summarize_run(metadata, load_binary_file(trace_path), trace_path))
    if not rows:
        print("No complete run metadata found.")
        return None
    per_run = pd.DataFrame(rows).sort_values(["campaign", "receiver", "requested_rate_pps",
                                              "batch_size", "repetition_id"])
    aggregate = aggregate_repetitions(per_run)
    per_run_path, aggregate_path = root / "per_run_summary.csv", root / "repetition_summary.csv"
    per_run.to_csv(per_run_path, index=False)
    aggregate.to_csv(aggregate_path, index=False)
    print(f"Per-run summary: {per_run_path}")
    print(f"Repetition aggregate: {aggregate_path}")
    return per_run_path, aggregate_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dir", help="session directory")
    args = parser.parse_args()
    process_directory(args.dir)
