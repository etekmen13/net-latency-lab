from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from generate_claim_evidence import generate as generate_claims
from generate_summary import (_latency_fields, aggregate_repetitions, linear_quantile,
                              minimum_samples_for, summarize_run, tail_quantile,
                              validate_metadata)


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


# --- percentile numerics -----------------------------------------------------

def test_linear_quantile_matches_hand_computed_type_seven_values():
    # Sorted 1..100.  The linear rule places quantile q at position q*(n-1).
    values = pd.Series(np.arange(1, 101, dtype=float))
    assert linear_quantile(values, 0.5) == pytest.approx(50.5)   # 49.5 -> 50 + .5*(51-50)
    assert linear_quantile(values, 0.99) == pytest.approx(99.01)  # 98.01 -> 99 + .01*(100-99)
    values = pd.Series(np.arange(1000, dtype=float))
    assert linear_quantile(values, 0.999) == pytest.approx(998.001)


def test_tail_quantile_refuses_a_sample_too_small_for_the_tail():
    assert minimum_samples_for(0.5) == 2
    assert minimum_samples_for(0.99) == 100
    assert minimum_samples_for(0.999) == 1000
    small = pd.Series(np.arange(50, dtype=float))
    # 50 samples cannot express a p99: the "quantile" is just the maximum.
    assert tail_quantile(small, 0.99) is None
    assert tail_quantile(small, 0.5) == pytest.approx(24.5)
    assert tail_quantile(pd.Series(np.arange(1000, dtype=float)), 0.999) == pytest.approx(998.001)


def synthetic_trace(count: int, latency_us: np.ndarray) -> pd.DataFrame:
    rx_ns = (1_700_000_000_000_000_000 + np.arange(count) * 1_000_000).astype("int64")
    tx_ns = rx_ns - (latency_us * 1000.0).astype("int64")
    return pd.DataFrame({
        "seq": np.arange(count), "tx_ns": tx_ns, "rx_ns": rx_ns,
        "processing_start_ns": rx_ns, "processing_finish_ns": rx_ns,
        "receive_latency_us": (rx_ns - tx_ns) / 1000.0,
        "application_queue_delay_us": np.zeros(count),
        "processing_time_us": np.zeros(count),
        "total_application_latency_us": (rx_ns - tx_ns) / 1000.0,
    })


def test_latency_fields_report_known_percentiles_and_sample_count():
    """1000 samples: 990 at 100 µs, 9 at 500 µs, 1 at 900 µs.

    The elevated samples are spread one per drift-estimation chunk so every
    chunk minimum is exactly 100 µs and the fitted drift slope is zero, leaving
    the percentiles hand-computable on the sorted values:
      p50  -> position 0.5*999   = 499.5  -> 100
      p99  -> position 0.99*999  = 989.01 -> 100 + .01*(500-100) = 104.0
      p99.9-> position 0.999*999 = 998.001-> 500 + .001*(900-500) = 500.4
    """
    latency = np.full(1000, 100.0)
    elevated = [50 * chunk + 25 for chunk in range(10)]
    latency[elevated[:9]] = 500.0
    latency[elevated[9]] = 900.0
    fields = _latency_fields(synthetic_trace(1000, latency))
    assert fields["latency_sample_count"] == 1000
    assert fields["clock_correction_applied"] is True
    assert fields["clock_drift_slope_us_per_s"] == pytest.approx(0.0, abs=1e-9)
    assert fields["receive_latency_p50_us"] == pytest.approx(100.0, abs=1e-6)
    assert fields["receive_latency_p99_us"] == pytest.approx(104.0, abs=1e-6)
    assert fields["receive_latency_p999_us"] == pytest.approx(500.4, abs=1e-6)
    assert fields["receive_latency_min_us"] == pytest.approx(100.0, abs=1e-6)
    assert fields["receive_latency_max_us"] == pytest.approx(900.0, abs=1e-6)


def test_latency_fields_do_not_invent_a_tail_from_a_tiny_trace():
    fields = _latency_fields(synthetic_trace(20, np.arange(1, 21, dtype=float)))
    assert fields["latency_sample_count"] == 20
    assert fields["clock_correction_applied"] is False
    assert fields["receive_latency_p50_us"] == pytest.approx(10.5)
    assert fields["receive_latency_p99_us"] is None
    assert fields["receive_latency_p999_us"] is None


def test_latency_fields_of_a_throughput_run_are_empty_not_zero():
    fields = _latency_fields(pd.DataFrame())
    assert fields["latency_sample_count"] == 0
    assert fields["receive_latency_p50_us"] is None
    assert fields["receive_latency_p999_us"] is None


# --- sustainability gate -----------------------------------------------------

def sustainable_metadata(**overrides):
    metadata = valid_metadata()
    metadata["topology"] = "distributed_ethernet"
    metadata["run"].update(requested_rate_pps=1000, duration_seconds=1)
    metadata["sender_stats"].update(attempted_sends=1000, successful_sends=1000,
                                    failed_sends=0, elapsed_seconds=1.0,
                                    achieved_successful_send_pps=1000.0)
    metadata["receiver_stats"].update(datagrams_received=1000, valid_packets=1000,
                                      processed_packets=1000)
    for key, value in overrides.items():
        metadata[key].update(value) if isinstance(value, dict) else None
    return metadata


def test_sustainable_run_accepts_a_clean_run():
    row = summarize_run(sustainable_metadata(), pd.DataFrame(), Path("t.bin"))
    assert row["sustainable_run"] is True
    assert row["application_loss_pct"] == pytest.approx(0.0)


def test_sustainable_run_rejects_a_run_that_carried_no_traffic():
    """Zero packets at zero requested rate must not pass the gate vacuously."""
    metadata = sustainable_metadata()
    metadata["run"]["requested_rate_pps"] = 0
    metadata["sender_stats"].update(attempted_sends=0, successful_sends=0, failed_sends=0)
    metadata["receiver_stats"].update(datagrams_received=0, valid_packets=0,
                                      processed_packets=0)
    row = summarize_run(metadata, pd.DataFrame(), Path("t.bin"))
    assert row["application_loss_pct"] == 0.0
    assert row["sustainable_run"] is False


def test_window_rates_are_measured_over_the_receiver_monotonic_windows():
    """processed_pps credits drained packets to the sender's interval; the
    window rate does not, which is what the service-rate model needs."""
    metadata = sustainable_metadata()
    # The receiver processed all 1000 packets, but took 2 s to do it because it
    # kept draining for a second after the 1 s sender interval ended.
    metadata["receiver_stats"].update(
        first_receive_mono_ns=1_000_000_000, last_receive_mono_ns=2_000_000_000,
        first_processing_mono_ns=1_000_000_000, last_processing_mono_ns=3_000_000_000,
        receive_syscalls=25, valid_packets=1000)
    row = summarize_run(metadata, pd.DataFrame(), Path("t.bin"))
    assert row["processed_pps"] == pytest.approx(1000.0)      # sender interval
    assert row["processing_window_pps"] == pytest.approx(500.0)  # true 2 s window
    assert row["receive_window_pps"] == pytest.approx(1000.0)
    assert row["packets_per_receive_syscall"] == pytest.approx(40.0)


def test_window_rates_are_none_when_the_window_is_unusable():
    metadata = sustainable_metadata()
    metadata["receiver_stats"].update(first_receive_mono_ns=0, last_receive_mono_ns=0,
                                      receive_syscalls=0)
    row = summarize_run(metadata, pd.DataFrame(), Path("t.bin"))
    assert row["receive_window_pps"] is None
    assert row["packets_per_receive_syscall"] is None


def test_zero_loss_is_reported_separately_from_the_tenth_of_a_percent_rule():
    """0.1% is 4500 packets at 150 kpps over 30 s, so it is not "zero loss"."""
    metadata = sustainable_metadata()
    assert summarize_run(metadata, pd.DataFrame(), Path("t.bin"))["zero_loss_run"] is True
    metadata["receiver_stats"].update(processed_packets=999, spsc_overflow=1)
    row = summarize_run(metadata, pd.DataFrame(), Path("t.bin"))
    assert row["sustainable_run"] is True   # 0.1% still passes the campaign rule
    assert row["zero_loss_run"] is False    # but it is not zero


def test_sustainable_run_rejects_loss_above_the_tenth_of_a_percent_rule():
    metadata = sustainable_metadata()
    metadata["receiver_stats"].update(processed_packets=998, spsc_overflow=2)
    row = summarize_run(metadata, pd.DataFrame(), Path("t.bin"))
    assert row["application_loss_pct"] == pytest.approx(0.2)
    assert row["sustainable_run"] is False


def test_aggregate_records_metrics_missing_from_some_repetitions():
    rows = []
    for repetition, latency in enumerate([1.0, 5.0, None], 1):
        rows.append({"topology": "distributed_ethernet", "receiver": "baseline",
                     "requested_rate_pps": 1000, "repetition_id": repetition,
                     "receive_latency_p99_us": latency})
    aggregate = aggregate_repetitions(pd.DataFrame(rows)).iloc[0]
    assert aggregate.repetitions == 3
    assert aggregate.receive_latency_p99_us_median == pytest.approx(3.0)
    assert aggregate.metrics_with_missing_repetitions == "receive_latency_p99_us:2/3"


# --- resume-claim gate -------------------------------------------------------

def claim_run(receiver, batch, rate, processed, repetition, *, sustainable=True,
              loss=0.0, topology="distributed_ethernet"):
    return {"campaign": "raw", "topology": topology, "receiver": receiver,
            "batch_size": batch, "requested_rate_pps": rate,
            "processed_pps": processed, "application_loss_pct": loss,
            "run_valid": True, "sustainable_run": sustainable,
            "run_id": f"{receiver}-b{batch}-{rate}-r{repetition}"}


def claim_sessions(tmp_path, *, saturated=True, tie=False, topology="distributed_ethernet",
                   batched_syscalls=0.125, batched_cycles=4000.0):
    rates = {"baseline": 100_000, "batched": 100_000 if tie else 200_000,
             "threaded": 100_000 if tie else 150_000}
    rows = []
    for receiver, rate in rates.items():
        for repetition in range(1, 6):
            rows.append(claim_run(receiver, 8, rate, rate + repetition * 1e-3,
                                  repetition, topology=topology))
            if saturated:
                rows.append(claim_run(receiver, 8, rate * 2, rate, repetition,
                                      sustainable=False, loss=9.0, topology=topology))
    measurement = tmp_path / "measurement"
    measurement.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(measurement / "per_run_summary.csv", index=False)

    profile_rows = []
    counters = {"baseline": (1.0, 6000.0), "batched": (batched_syscalls, batched_cycles),
                "threaded": (1.0, 9000.0)}
    for receiver, (syscalls, cycles) in counters.items():
        for repetition in range(3):
            profile_rows.append({"run_id": f"{receiver}-{repetition}", "receiver": receiver,
                                 "batch_size": 8, "processed_packets": 1000,
                                 "receive_syscalls_per_packet": syscalls,
                                 "cycles_per_packet": cycles})
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    pd.DataFrame(profile_rows).to_csv(profile / "profile_summary.csv", index=False)
    return measurement, profile


def claim_rows(tmp_path, **kwargs):
    measurement, profile = claim_sessions(tmp_path, **kwargs)
    output = generate_claims(measurement, profile, tmp_path / "claim_evidence.csv")
    return pd.read_csv(output).set_index("candidate")


def test_claim_gate_emits_a_sentence_only_for_a_supported_mechanism(tmp_path):
    rows = claim_rows(tmp_path)
    batched = rows.loc["batched"]
    assert batched.claim_passes
    assert batched.improvement_ratio == pytest.approx(2.0, rel=1e-6)
    assert batched.baseline_repetitions == 5 and batched.candidate_repetitions == 5
    assert batched.candidate_application_loss_pct_max <= 0.1
    assert batched.resume_claim.startswith("On two Raspberry Pi 4s")
    # Threaded is faster but burns more cycles per packet: no mechanism, no claim.
    threaded = rows.loc["threaded"]
    assert not threaded.claim_passes
    assert pd.isna(threaded.resume_claim) or threaded.resume_claim == ""
    assert "cycles per packet not reduced" in threaded.claim_blockers


def test_claim_gate_rejects_a_sender_limited_tie(tmp_path):
    """All receivers pinned to the same offered rate is noise, not a result."""
    rows = claim_rows(tmp_path, saturated=False, tie=True)
    for candidate in ("batched", "threaded"):
        row = rows.loc[candidate]
        assert not row.claim_passes
        assert not row.candidate_saturated
        assert "sender-limited" in row.claim_blockers
        assert "minimum effect size" in row.claim_blockers


def test_claim_gate_rejects_a_syscall_tie_at_floating_point_precision(tmp_path):
    rows = claim_rows(tmp_path, batched_syscalls=1.0 - 1e-9)
    row = rows.loc["batched"]
    assert not row.claim_passes
    assert "receive syscalls per packet not meaningfully reduced" in row.claim_blockers


def test_claim_gate_rejects_missing_profile_counters(tmp_path):
    rows = claim_rows(tmp_path, batched_syscalls=float("nan"))
    row = rows.loc["batched"]
    assert not row.claim_passes
    assert "receive syscalls per packet unavailable" in row.claim_blockers


def test_claim_gate_refuses_loopback_data_entirely(tmp_path):
    measurement, profile = claim_sessions(tmp_path, topology="local_loopback")
    with pytest.raises(ValueError, match="sustainable"):
        generate_claims(measurement, profile, tmp_path / "claim_evidence.csv")
