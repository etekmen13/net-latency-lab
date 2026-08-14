from __future__ import annotations

import copy
import json
import signal
import struct
from pathlib import Path

import pytest
import yaml

from main import (counter_deltas, parse_udp_rcvbuf_errors, receiver_command,
                  sender_command, validate_config, run_campaign, ProcessHandle,
                  LocalNode, NodeController, RemoteNode)

ROOT = Path(__file__).resolve().parents[1]


def debug_config():
    return yaml.safe_load((ROOT / "config_debug.yaml").read_text())


def test_named_configs_validate_and_local_is_safe_default():
    local = validate_config(debug_config())
    assert local["global"]["topology"] == "local_loopback"
    assert local["global"]["nodes"]["receiver"]["interface"] == "lo"
    distributed = yaml.safe_load((ROOT / "config_distributed.yaml").read_text())
    assert validate_config(distributed)["global"]["topology"] == "distributed_ethernet"


def test_inconsistent_topology_and_runtime_are_rejected():
    config = debug_config(); config["global"]["topology"] = "distributed_ethernet"
    with pytest.raises(ValueError, match="distributed"):
        validate_config(config)
    config = debug_config(); config["global"]["runtime"]["repetitions"] = 0
    with pytest.raises(ValueError, match="repetitions"):
        validate_config(config)


def test_command_construction_has_stats_runtime_and_variant_options():
    config = debug_config(); runtime = config["global"]["runtime"]
    threaded = config["benchmarks"][2]
    rx = receiver_command("/project", threaded, runtime, "/tmp/run.bin", "/tmp/rx.json", 8)
    tx = sender_command("/project", "127.0.0.1", threaded, runtime, "/tmp/tx.json", 1000, 1)
    assert rx[0] == "/project/build/dev/receiver_threaded"
    assert "--stats" in rx and "--worker-cpu" not in rx and rx[rx.index("--batch") + 1] == "8"
    assert "--payload-size" in tx and "--stats" in tx and "--rate" in tx


def test_udp_counter_parser_and_separate_deltas():
    text = "Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors\nUdp: 10 0 2 11 7\n"
    assert parse_udp_rcvbuf_errors(text) == 7
    before = {"udp_rcvbuf_errors": {"value": 7, "error": None},
              "nic_rx_dropped": {"value": None, "error": "unavailable"}}
    after = {"udp_rcvbuf_errors": {"value": 9, "error": None},
             "nic_rx_dropped": {"value": None, "error": "unavailable"}}
    delta = counter_deltas(before, after)
    assert delta["udp_rcvbuf_errors"] == {"before": 7, "after": 9, "delta": 2, "error": None}
    assert delta["nic_rx_dropped"]["delta"] is None
    assert delta["nic_rx_dropped"]["error"] == "unavailable"


def test_node_cleanup_uses_explicit_paths_and_tolerates_missing_files(tmp_path):
    existing = tmp_path / "artifact with spaces"
    existing.write_text("data")
    missing = tmp_path / "already-missing"
    LocalNode().remove_files([str(existing), str(missing)])
    assert not existing.exists()

    remote = object.__new__(RemoteNode)
    commands = []
    remote._exec = lambda command: commands.append(command)
    remote.remove_files(["/tmp/plain", "/tmp/name with spaces", "/tmp/not;another-command"])
    assert commands == [
        "rm -f -- /tmp/plain '/tmp/name with spaces' '/tmp/not;another-command'"
    ]


class FakeNode(NodeController):
    def __init__(self, receiver_status=0, sender_status=0, fetch_failure=None,
                 invalid_rx_json=False, cleanup_error=False, metadata_root=None):
        self.next_pid = 100
        self.handles = []
        self.signals = []
        self.receiver_status = receiver_status
        self.sender_status = sender_status
        self.fetch_failure = fetch_failure
        self.invalid_rx_json = invalid_rx_json
        self.cleanup_error = cleanup_error
        self.metadata_root = metadata_root
        self.cleanup_requests = []
        self.metadata_existed_at_cleanup = []

    def run(self, command, timeout=30.0):
        return ""

    def start_process(self, command, log_path):
        handle = ProcessHandle(self.next_pid, list(command), log_path)
        self.next_pid += 1; self.handles.append(handle); return handle

    def wait_process(self, handle, timeout):
        if any("receiver_" in part for part in handle.command):
            return self.receiver_status
        return self.sender_status

    def signal_process(self, handle, signum):
        self.signals.append((handle.pid, signum))

    def snapshot_counters(self, interface):
        return {"udp_rcvbuf_errors": {"value": 10, "error": None},
                "nic_rx_dropped": {"value": None, "error": "not available"}}

    def fetch_file(self, source, destination):
        if self.fetch_failure and source.endswith(self.fetch_failure):
            raise RuntimeError(f"transfer failed: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.endswith(".bin"):
            header = struct.pack("<8sHHI", b"NLLOG\x00\r\n", 1, 16, 36)
            record = struct.pack("<IQQQQ", 0, 100, 110, 115, 120)
            destination.write_bytes(header + record)
        elif source.endswith("_rx.json"):
            destination.write_text("not json" if self.invalid_rx_json else json.dumps({"variant": "baseline", "datagrams_received": 1,
                "valid_packets": 1, "unique_valid_packets": 1,
                "receive_sequence_gaps": 0, "receive_duplicates": 0,
                "receive_reordered": 0, "processed_packets": 1,
                "unique_processed_packets": 1, "processed_sequence_gaps": 0,
                "processed_duplicates": 0, "processed_reordered": 0,
                "short_packets": 0, "invalid_magic": 0, "spsc_overflow": 0,
                "queue_depth_at_shutdown": 0}))
        elif source.endswith("_perf_stat.csv"):
            destination.write_text("1,,cycles,100.00,100.00\n")
        elif source.endswith(".perf.data"):
            destination.write_bytes(b"perf data")
        else:
            destination.write_text(json.dumps({"attempted_sends": 1, "successful_sends": 1,
                "failed_sends": 0, "elapsed_seconds": 1.0,
                "achieved_successful_send_pps": 1.0}))

    def remove_files(self, paths):
        self.cleanup_requests.append(list(paths))
        if self.metadata_root is not None:
            run_ids = [path.name.removesuffix("_meta.json")
                       for path in self.metadata_root.glob("**/*_meta.json")]
            self.metadata_existed_at_cleanup.append(any(
                all(f"_{run_id}" in Path(remote).name for remote in paths)
                for run_id in run_ids))
        if self.cleanup_error:
            raise RuntimeError("cleanup failed")


def one_benchmark_config(tmp_path):
    config = debug_config()
    config["global"]["metadata_collection"] = False
    config["global"]["local_project_root"] = str(tmp_path)
    config["global"]["local_data_dir"] = "sessions"
    config["global"]["runtime"]["repetitions"] = 2
    config["benchmarks"] = [config["benchmarks"][0]]
    return config


def test_fake_controller_pid_lifecycle_repetitions_and_metadata(monkeypatch, tmp_path):
    fake = FakeNode(metadata_root=tmp_path)
    monkeypatch.setattr("main.get_node", lambda host, user: fake)
    monkeypatch.setattr("main.time.sleep", lambda seconds: None)
    config_path = tmp_path / "config.yaml"
    config = one_benchmark_config(tmp_path)
    config_path.write_text(yaml.safe_dump(config))
    session = run_campaign(config, config_path, skip_build=True)
    manifest = json.loads((session / "session_manifest.json").read_text())
    assert len(manifest["runs"]) == 2
    assert len({run["run_id"] for run in manifest["runs"]}) == 2
    assert len(fake.handles) == 4
    receiver_pids = [handle.pid for handle in fake.handles if "receiver_baseline" in handle.command[0]]
    assert fake.signals == [(pid, signal.SIGINT) for pid in receiver_pids]
    metadata = json.loads(next(session.glob("**/*_meta.json")).read_text())
    assert metadata["topology"] == "local_loopback"
    assert metadata["processes"]["receiver_pid"] in receiver_pids
    assert metadata["counters"]["nic_rx_dropped"]["delta"] is None
    assert metadata["counters"]["nic_rx_dropped"]["error"] == "not available"
    assert (session / "config_snapshot.yaml").exists()
    assert len(fake.cleanup_requests) == 4
    assert all(fake.metadata_existed_at_cleanup)
    ordinary_suffixes = {
        ".bin", "_rx.json", "_receiver.log", "_receiver.log.status",
        "_tx.json", "_sender.log", "_sender.log.status",
    }
    for request_pair in zip(fake.cleanup_requests[::2], fake.cleanup_requests[1::2]):
        cleaned = request_pair[0] + request_pair[1]
        assert len(cleaned) == len(ordinary_suffixes)
        assert all(path.startswith("/tmp/nll_") for path in cleaned)
        assert {next(suffix for suffix in ordinary_suffixes if path.endswith(suffix))
                for path in cleaned} == ordinary_suffixes


def test_fake_controller_receiver_failure_propagates(monkeypatch, tmp_path):
    fake = FakeNode(receiver_status=1)
    monkeypatch.setattr("main.get_node", lambda host, user: fake)
    monkeypatch.setattr("main.time.sleep", lambda seconds: None)
    config = one_benchmark_config(tmp_path); config["global"]["runtime"]["repetitions"] = 1
    config_path = tmp_path / "config.yaml"; config_path.write_text(yaml.safe_dump(config))
    with pytest.raises(RuntimeError, match="Receiver failed"):
        run_campaign(config, config_path, skip_build=True)
    assert fake.cleanup_requests == []


@pytest.mark.parametrize("profile, profile_suffix", [
    ("stat", "_perf_stat.csv"),
    ("record", "_perf.data"),
])
def test_profile_artifact_is_cleaned_after_local_metadata(monkeypatch, tmp_path,
                                                          profile, profile_suffix):
    fake = FakeNode(metadata_root=tmp_path)
    monkeypatch.setattr("main.get_node", lambda host, user: fake)
    monkeypatch.setattr("main.time.sleep", lambda seconds: None)
    config = one_benchmark_config(tmp_path)
    config["global"]["runtime"]["repetitions"] = 1
    config["benchmarks"][0]["profile"] = profile
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))

    run_campaign(config, config_path, skip_build=True)

    assert len(fake.cleanup_requests) == 2
    assert any(path.endswith(profile_suffix) for path in fake.cleanup_requests[0])
    assert all(fake.metadata_existed_at_cleanup)


@pytest.mark.parametrize("failure", ["transfer", "parsing"])
def test_artifact_failure_preserves_remote_diagnostics(monkeypatch, tmp_path, failure):
    fake = FakeNode(fetch_failure="_rx.json" if failure == "transfer" else None,
                    invalid_rx_json=failure == "parsing")
    monkeypatch.setattr("main.get_node", lambda host, user: fake)
    monkeypatch.setattr("main.time.sleep", lambda seconds: None)
    config = one_benchmark_config(tmp_path)
    config["global"]["runtime"]["repetitions"] = 1
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))

    with pytest.raises((RuntimeError, ValueError, json.JSONDecodeError)):
        run_campaign(config, config_path, skip_build=True)

    assert fake.cleanup_requests == []


def test_cleanup_failure_propagates_and_stops_campaign(monkeypatch, tmp_path):
    fake = FakeNode(cleanup_error=True)
    monkeypatch.setattr("main.get_node", lambda host, user: fake)
    monkeypatch.setattr("main.time.sleep", lambda seconds: None)
    config = one_benchmark_config(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))

    with pytest.raises(RuntimeError, match="cleanup failed"):
        run_campaign(config, config_path, skip_build=True)

    assert len(fake.handles) == 2
    assert len(fake.cleanup_requests) == 1
