from __future__ import annotations

import pandas as pd
import pytest

from generate_summary import aggregate_repetitions, linear_quantile, validate_metadata


def valid_metadata():
    return {
        "schema_version": 1, "run_id": "run", "repetition_id": 1,
        "topology": "local_loopback",
        "run": {"receiver_variant": "baseline", "requested_rate_pps": 1000,
                "duration_seconds": 1, "payload_size": 64, "mode": "steady",
                "burst_size": 1, "batch_size": 1, "work_ns": 0},
        "sender_stats": {"attempted_sends": 10, "successful_sends": 9,
                         "failed_sends": 1, "elapsed_seconds": 1,
                         "achieved_successful_send_pps": 9},
        "receiver_stats": {"datagrams_received": 9, "valid_packets": 9,
                           "processed_packets": 9, "short_packets": 0,
                           "invalid_magic": 0, "spsc_overflow": 0},
        "counters": {}, "environment": {},
    }


def test_metadata_validation_accepts_complete_accounting():
    assert validate_metadata(valid_metadata())["run_id"] == "run"


def test_metadata_validation_rejects_inconsistent_sender_counts():
    metadata = valid_metadata(); metadata["sender_stats"]["failed_sends"] = 2
    with pytest.raises(ValueError, match="attempts"):
        validate_metadata(metadata)


def test_aggregate_uses_median_and_interquartile_spread():
    rows = []
    for repetition, latency in enumerate([1.0, 5.0, 9.0], 1):
        rows.append({"topology": "local_loopback", "receiver": "baseline",
                     "requested_rate_pps": 1000, "mode": "steady", "burst_size": 1,
                     "batch_size": 1, "payload_size": 64, "duration_seconds": 1,
                     "work_ns": 0, "repetition_id": repetition,
                     "receive_latency_p99_us": latency})
    aggregate = aggregate_repetitions(pd.DataFrame(rows)).iloc[0]
    assert aggregate.repetitions == 3
    assert aggregate.receive_latency_p99_us_median == 5
    assert aggregate.receive_latency_p99_us_q25 == 3
    assert aggregate.receive_latency_p99_us_q75 == 7


def test_linear_quantile_method():
    assert linear_quantile(pd.Series([0.0, 10.0]), 0.99) == pytest.approx(9.9)
