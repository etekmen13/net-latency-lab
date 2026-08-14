from __future__ import annotations

import struct

import numpy as np
import pandas as pd
import pytest

from latency_utils import (HEADER_FORMAT, LEGACY_FORMAT, LOG_MAGIC, VERSIONED_FORMAT,
                           load_binary_file, remove_clock_drift, sequence_statistics)


def test_loads_legacy_log_and_derives_metrics(tmp_path):
    path = tmp_path / "legacy.bin"
    path.write_bytes(struct.pack(LEGACY_FORMAT, 7, 1000, 1600, 600))
    frame = load_binary_file(path)
    assert frame.loc[0, "log_format_version"] == 0
    assert frame.loc[0, "receive_latency_ns"] == 600
    assert frame.loc[0, "application_queue_delay_ns"] == 0
    assert frame.loc[0, "processing_time_ns"] == 0
    assert frame.loc[0, "total_application_latency_ns"] == 600


def test_loads_versioned_log_and_timestamp_identities(tmp_path):
    path = tmp_path / "v1.bin"
    header = struct.pack(HEADER_FORMAT, LOG_MAGIC, 1, 16, 36)
    path.write_bytes(header + struct.pack(VERSIONED_FORMAT, 9, 1000, 1300, 1500, 1900))
    row = load_binary_file(path).iloc[0]
    assert row.receive_latency_ns == 300
    assert row.application_queue_delay_ns == 200
    assert row.processing_time_ns == 400
    assert row.total_application_latency_ns == 900
    assert row.total_application_latency_ns == row.receive_latency_ns + row.application_queue_delay_ns + row.processing_time_ns


def test_truncated_log_is_rejected(tmp_path):
    path = tmp_path / "broken.bin"
    path.write_bytes(struct.pack(HEADER_FORMAT, LOG_MAGIC, 1, 16, 36) + b"x")
    with pytest.raises(ValueError, match="Truncated"):
        load_binary_file(path)


def test_unsupported_version_is_rejected(tmp_path):
    path = tmp_path / "future.bin"
    path.write_bytes(struct.pack(HEADER_FORMAT, LOG_MAGIC, 2, 16, 36))
    with pytest.raises(ValueError, match="Unsupported"):
        load_binary_file(path)


def test_short_run_drift_contract_preserves_latency():
    frame = pd.DataFrame({"seq": range(3), "tx_ns": [1, 2, 3], "rx_ns": [11, 22, 33],
                          "processing_start_ns": [11, 22, 33], "processing_finish_ns": [11, 22, 33]})
    frame["receive_latency_us"] = (frame.rx_ns - frame.tx_ns) / 1000
    corrected, slope = remove_clock_drift(frame)
    assert slope == 0.0
    assert corrected.latency_corrected_us.tolist() == frame.receive_latency_us.tolist()
    assert not corrected.clock_correction_applied.any()
    assert "jitter_us" in corrected


def test_sequence_loss_duplicates_and_reordering():
    stats = sequence_statistics(pd.Series([10, 11, 13, 13, 12, 15]))
    assert stats == {"sequence_gap_loss": 1, "duplicates": 1, "reordered": 1}


def test_linear_quantiles_are_known():
    values = pd.Series(np.arange(1000, dtype=float))
    assert values.quantile(0.5, interpolation="linear") == pytest.approx(499.5)
    assert values.quantile(0.99, interpolation="linear") == pytest.approx(989.01)
    assert values.quantile(0.999, interpolation="linear") == pytest.approx(998.001)
