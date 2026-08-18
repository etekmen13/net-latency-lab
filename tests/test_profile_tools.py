from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import pytest

from profile_tools import (parse_perf_stat, select_representatives,
                           summarize_profiles, sustainable_groups,
                           validate_profile_session)

CONFIGURATIONS = [
    ("baseline", 1, 100_000, 99_900),
    ("batched", 4, 200_000, 198_000),
    ("batched", 8, 200_000, 200_000),  # within 2%; smaller batch must win
    ("threaded", 16, 300_000, 299_000),
]


def _run(receiver, batch, requested, processed, repetition, *, sustainable=True,
         topology="distributed_ethernet", loss=0.0):
    return {"campaign": "raw", "topology": topology, "receiver": receiver,
            "batch_size": batch, "requested_rate_pps": requested,
            "processed_pps": processed + repetition,
            "application_loss_pct": loss,
            "run_valid": True, "sustainable_run": sustainable,
            "run_id": f"{receiver}-{batch}-{requested}-{repetition}"}


def profile_fixture(saturated: bool = True) -> pd.DataFrame:
    rows = []
    for receiver, batch, requested, processed in CONFIGURATIONS:
        for repetition in range(1, 6):
            rows.append(_run(receiver, batch, requested, processed, repetition))
            if saturated:
                # A strictly higher offered rate that the receiver failed: this
                # is what makes the selected rate receiver-limited evidence.
                rows.append(_run(receiver, batch, requested * 2, processed,
                                 repetition, sustainable=False, loss=5.0))
    return pd.DataFrame(rows)


def test_representative_selection_uses_five_runs_and_two_percent_tiebreak():
    selected = {row["receiver"]: row for row in select_representatives(profile_fixture())}
    assert selected["baseline"]["batch_size"] == 1
    assert selected["batched"]["batch_size"] == 4
    assert selected["threaded"]["batch_size"] == 16
    assert len(selected["batched"]["run_ids"].split("|")) == 5


def _workload_fixture() -> pd.DataFrame:
    """Two workloads under one campaign: 0 ns reaches higher rates than 5 us."""
    rows = []
    for receiver, batch in (("baseline", 1), ("batched", 64), ("threaded", 64)):
        for work_ns, requested, processed in ((0, 350_000, 349_000),
                                              (5000, 150_000, 149_000)):
            for repetition in range(1, 6):
                row = _run(receiver, batch, requested, processed, repetition)
                row["work_ns"] = work_ns
                row["run_id"] = f"{receiver}-{batch}-{work_ns}-{requested}-{repetition}"
                rows.append(row)
                failing = _run(receiver, batch, requested * 2, processed,
                               repetition, sustainable=False, loss=5.0)
                failing["work_ns"] = work_ns
                failing["run_id"] = f"{receiver}-{batch}-{work_ns}-hi-{repetition}"
                rows.append(failing)
    return pd.DataFrame(rows)


def test_representative_selection_never_mixes_two_workloads():
    """A 0 ns baseline must not be paired against a 5 us candidate.

    The unrestricted rate is always higher without per-packet work, so a
    selection that groups only by batch size silently picks the 0 ns row for
    every receiver -- and nothing downstream compares the two rows' work_ns.
    """
    selected = select_representatives(_workload_fixture(), work_ns=5000)
    assert {row["work_ns"] for row in selected} == {5000}
    assert {int(row["requested_rate_pps"]) for row in selected} == {150_000}

    unrestricted = select_representatives(_workload_fixture())
    assert len({row["work_ns"] for row in unrestricted}) == 1


def test_saturation_is_judged_within_one_workload():
    """A 5 us sweep topping out at 300k is not "saturated" because a separate
    0 ns sweep in the same campaign happened to reach 700k."""
    groups = sustainable_groups(_workload_fixture())
    for _, row in groups.iterrows():
        assert row["max_tested_rate_pps"] == row["requested_rate_pps"] * 2
        assert bool(row["saturated"]) is True


def test_sustainable_groups_reject_loopback_rows():
    frame = profile_fixture()
    frame.loc[frame.receiver == "baseline", "topology"] = "local_loopback"
    assert sustainable_groups(frame).receiver.unique().tolist() == ["batched", "threaded"]


def test_sustainable_groups_require_topology_column():
    frame = profile_fixture().drop(columns=["topology"])
    with pytest.raises(ValueError, match="topology"):
        sustainable_groups(frame)


def test_sustainable_groups_mark_unsaturated_sweeps():
    saturated = sustainable_groups(profile_fixture(saturated=True))
    assert saturated.saturated.all()
    # A sweep whose highest offered rate still passed never found the knee.
    unsaturated = sustainable_groups(profile_fixture(saturated=False))
    assert not unsaturated.saturated.any()


def test_sustainable_groups_do_not_merge_different_sample_rates():
    frame = profile_fixture(saturated=False)
    frame["sample_every"] = 0
    # Three unsampled plus two sampled runs must not look like five repetitions.
    mask = (frame.receiver == "baseline") & (frame.run_id.str.endswith(("-4", "-5")))
    frame.loc[mask, "sample_every"] = 100
    assert sustainable_groups(frame)[lambda d: d.receiver == "baseline"].empty


def test_perf_parser_preserves_unsupported_events(tmp_path: Path):
    path = tmp_path / "perf.csv"
    path.write_text(
        "1000,,cycles,100.00,100.00\n"
        "2000,,instructions,100.00,100.00\n"
        "<not supported>,,cache-misses,0.00,0.00\n")
    counters = parse_perf_stat(path)
    assert counters["cycles"] == 1000
    assert counters["instructions"] == 2000
    assert counters["cache-misses"] is None


def complete_profile_session(tmp_path: Path) -> Path:
    session = tmp_path / "session_profile"
    session.mkdir()
    runs = []
    for number in range(12):
        mode = "stat" if number < 9 else "record"
        run_id = f"profile_{mode}_{number:02d}"
        run_dir = session / run_id
        run_dir.mkdir()
        trace = run_dir / f"{run_id}.bin"
        stats = run_dir / f"{run_id}_rx_stats.json"
        metadata_path = run_dir / f"{run_id}_meta.json"
        trace.write_bytes(b"trace")
        stats.write_text(json.dumps({"unique_processed_packets": 10}))
        profile = {"mode": mode}
        if mode == "stat":
            artifact = run_dir / f"{run_id}_perf_stat.csv"
            artifact.write_text("1,,cycles,100,100\n")
            profile["artifact"] = artifact.name
        else:
            artifact = run_dir / f"{run_id}.perf.data"
            report = run_dir / f"{run_id}_perf_report.txt"
            artifact.write_bytes(b"perf")
            report.write_text("report\n")
            profile.update({"perf_data": artifact.name, "report": report.name})
        metadata = {
            "run_id": run_id,
            "run": {"campaign": "profile"},
            "validity": {"valid": True, "reasons": []},
            "processes": {"receiver_pid": 1000 + number * 2,
                          "profile_wrapper_pid": 1001 + number * 2},
            "receiver_stats": {"unique_processed_packets": 10},
            "profile": profile,
        }
        metadata_path.write_text(json.dumps(metadata))
        runs.append({"run_id": run_id,
                     "metadata": str(metadata_path.relative_to(session)),
                     "trace": str(trace.relative_to(session))})
    (session / "session_manifest.json").write_text(json.dumps({"runs": runs}))
    return session


def mutate_metadata(session: Path, index: int, mutation) -> None:
    manifest = json.loads((session / "session_manifest.json").read_text())
    path = session / manifest["runs"][index]["metadata"]
    metadata = json.loads(path.read_text())
    mutation(metadata, path)
    path.write_text(json.dumps(metadata))


def test_validate_complete_profile_session(tmp_path: Path):
    session = complete_profile_session(tmp_path)
    assert validate_profile_session(session) == session.resolve()


def test_validate_rejects_wrong_run_count(tmp_path: Path):
    session = complete_profile_session(tmp_path)
    manifest_path = session / "session_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["runs"].pop()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="exactly 12"):
        validate_profile_session(session)


def test_validate_rejects_invalid_run(tmp_path: Path):
    session = complete_profile_session(tmp_path)
    mutate_metadata(session, 0,
                    lambda metadata, _: metadata["validity"].update(valid=False))
    with pytest.raises(ValueError, match="run is invalid"):
        validate_profile_session(session)


def test_validate_rejects_empty_artifact(tmp_path: Path):
    session = complete_profile_session(tmp_path)
    def empty_artifact(metadata, metadata_path):
        (metadata_path.parent / metadata["profile"]["artifact"]).write_bytes(b"")
    mutate_metadata(session, 0, empty_artifact)
    with pytest.raises(ValueError, match="Missing or empty perf stat artifact"):
        validate_profile_session(session)


def test_validate_rejects_malformed_receiver_stats(tmp_path: Path):
    session = complete_profile_session(tmp_path)
    manifest = json.loads((session / "session_manifest.json").read_text())
    metadata_path = session / manifest["runs"][0]["metadata"]
    metadata_path.with_name(metadata_path.name.replace("_meta.json", "_rx_stats.json")).write_text("{")
    with pytest.raises(ValueError, match="Malformed receiver stats"):
        validate_profile_session(session)


def test_validate_rejects_pid_aliasing(tmp_path: Path):
    session = complete_profile_session(tmp_path)
    mutate_metadata(session, 0, lambda metadata, _: metadata["processes"].update(
        profile_wrapper_pid=metadata["processes"]["receiver_pid"]))
    with pytest.raises(ValueError, match="PIDs alias"):
        validate_profile_session(session)


def test_validate_rejects_missing_record_report(tmp_path: Path):
    session = complete_profile_session(tmp_path)
    def remove_report(metadata, metadata_path):
        (metadata_path.parent / metadata["profile"]["report"]).unlink()
    mutate_metadata(session, 9, remove_report)
    with pytest.raises(ValueError, match="Missing or empty perf record report"):
        validate_profile_session(session)


def stat_session(tmp_path: Path, perf_csv: str) -> Path:
    session = tmp_path / "stat_session"
    run_dir = session / "profile_stat_batched"
    run_dir.mkdir(parents=True)
    (run_dir / "run_perf_stat.csv").write_text(perf_csv)
    (run_dir / "run_meta.json").write_text(json.dumps({
        "run_id": "run", "run": {"receiver_variant": "batched", "batch_size": 8},
        "receiver_stats": {"unique_processed_packets": 1000},
        "sender_stats": {"elapsed_seconds": 10.0},
        "profile": {"mode": "stat", "artifact": "run_perf_stat.csv"},
    }))
    return session


COMPLETE_PERF_CSV = (
    "1000,,task-clock,100,100.00\n"
    "20000,,cycles,100,100.00\n"
    "30000,,instructions,100,100.00\n"
    "1,,branches,100,100.00\n"
    "1,,branch-misses,100,100.00\n"
    "1,,cache-references,100,100.00\n"
    "1,,cache-misses,100,100.00\n"
    "500,,context-switches,100,100.00\n"
    "0,,cpu-migrations,100,100.00\n"
    "1,,page-faults,100,100.00\n"
    "0,,syscalls:sys_enter_recvfrom,100,100.00\n"
    "125,,syscalls:sys_enter_recvmmsg,100,100.00\n")


def test_summarize_keeps_a_real_zero_receive_counter(tmp_path: Path):
    """recvfrom == 0 is a measurement, not a missing value."""
    summary = pd.read_csv(summarize_profiles(stat_session(tmp_path, COMPLETE_PERF_CSV)))
    row = summary.iloc[0]
    assert row.receive_syscalls_per_packet == pytest.approx(0.125)
    assert row.context_switches_per_second == pytest.approx(50.0)
    assert pd.isna(row.unavailable_events)


def test_summarize_never_sums_an_unavailable_counter_as_zero(tmp_path: Path):
    perf_csv = COMPLETE_PERF_CSV.replace(
        "0,,syscalls:sys_enter_recvfrom,100,100.00",
        "<not supported>,,syscalls:sys_enter_recvfrom,0,0.00").replace(
        "500,,context-switches,100,100.00",
        "<not counted>,,context-switches,0,0.00")
    summary = pd.read_csv(summarize_profiles(stat_session(tmp_path, perf_csv)))
    row = summary.iloc[0]
    assert pd.isna(row.receive_syscalls_per_packet)
    assert pd.isna(row.context_switches_per_second)
    assert row.unavailable_events == "context-switches; syscalls:sys_enter_recvfrom"
