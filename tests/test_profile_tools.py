from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import pytest

from profile_tools import (parse_perf_stat, select_representatives,
                           validate_profile_session)


def profile_fixture() -> pd.DataFrame:
    rows = []
    configurations = [
        ("baseline", 1, 100_000, 99_900),
        ("batched", 4, 200_000, 198_000),
        ("batched", 8, 200_000, 200_000),  # within 2%; smaller batch must win
        ("threaded", 16, 300_000, 299_000),
    ]
    for receiver, batch, requested, processed in configurations:
        for repetition in range(1, 6):
            rows.append({"campaign": "raw", "receiver": receiver,
                         "batch_size": batch, "requested_rate_pps": requested,
                         "processed_pps": processed + repetition,
                         "run_valid": True, "sustainable_run": True,
                         "run_id": f"{receiver}-{batch}-{repetition}"})
    return pd.DataFrame(rows)


def test_representative_selection_uses_five_runs_and_two_percent_tiebreak():
    selected = {row["receiver"]: row for row in select_representatives(profile_fixture())}
    assert selected["baseline"]["batch_size"] == 1
    assert selected["batched"]["batch_size"] == 4
    assert selected["threaded"]["batch_size"] == 16
    assert len(selected["batched"]["run_ids"].split("|")) == 5


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
