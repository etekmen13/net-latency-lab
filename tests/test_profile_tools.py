from __future__ import annotations

from pathlib import Path

import pandas as pd

from profile_tools import parse_perf_stat, select_representatives


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
