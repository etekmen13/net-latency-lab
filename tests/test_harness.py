from __future__ import annotations

import copy
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from main import (counter_deltas, parse_udp_rcvbuf_errors, receiver_command,
                  sender_command, validate_config, run_campaign, ProcessHandle,
                  LocalNode, NodeController, RemoteNode, shutdown_process)

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


def test_remote_process_tracks_and_signals_child_pid_before_reading_status():
    remote = object.__new__(RemoteNode)
    commands = []
    responses = iter(["4100 4200", "", "0"])

    def fake_exec(command, timeout=30.0):
        commands.append(command)
        return next(responses)

    remote._exec = fake_exec
    handle = remote.start_process(["/project/receiver", "--flag"], "/tmp/run.log")
    assert handle.pid == 4200
    assert handle.monitor_pid == 4100
    assert "/tmp/run.log.pid" in commands[0]
    assert "setsid /project/receiver --flag" in commands[0]
    assert "trap " not in commands[0]

    remote.signal_process(handle, signal.SIGINT)
    assert commands[1] == "kill -INT -4200 2>/dev/null || true"
    assert remote.wait_process(handle, 1.0) == 0
    assert commands[2].startswith("if test -f /tmp/run.log.status;")
    assert "kill -0 4100" in commands[2]


def test_remote_profile_tracks_and_signals_workload_not_process_group():
    remote = object.__new__(RemoteNode)
    commands = []
    responses = iter(["4100 4200 4300", "", "0"])

    def fake_exec(command, timeout=30.0):
        commands.append(command)
        return next(responses)

    remote._exec = fake_exec
    command = ["perf", "record", "--", "/project/receiver_baseline", "--flag"]
    handle = remote.start_process(command, "/tmp/profile.log", workload_index=3)
    assert handle.wrapper_pid == 4200
    assert handle.workload_pid == 4300
    assert "/tmp/profile.log.workload.pid" in commands[0]
    assert "nll-workload" in commands[0]

    remote.signal_process(handle, signal.SIGINT)
    assert commands[1] == "kill -INT 4300 2>/dev/null || true"
    assert remote.wait_process(handle, 1.0) == 0


def test_remote_process_wrapper_records_signaled_child_exit(tmp_path):
    remote = object.__new__(RemoteNode)

    def local_shell_exec(command, timeout=30.0):
        result = subprocess.run(["sh", "-c", command], check=True,
                                capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()

    remote._exec = local_shell_exec
    log_path = str(tmp_path / "remote-process.log")
    handle = remote.start_process(
        [sys.executable, "-c",
         "import signal,time; signal.signal(signal.SIGINT, lambda *_: exit(0)); time.sleep(60)"],
        log_path)
    try:
        time.sleep(0.05)
        remote.signal_process(handle, signal.SIGINT)
        assert remote.wait_process(handle, 2.0) == 0
    finally:
        remote.signal_process(handle, signal.SIGTERM)
        remote.remove_files([log_path, f"{log_path}.status", f"{log_path}.pid"])


def process_tree_command(tmp_path):
    child_ready = tmp_path / "child.ready"
    child_interrupted = tmp_path / "child.interrupted"
    child_pid = tmp_path / "child.pid"
    child_code = (
        "import pathlib,signal,sys,time; "
        "ready=pathlib.Path(sys.argv[1]); interrupted=pathlib.Path(sys.argv[2]); "
        "signal.signal(signal.SIGINT, lambda *_: (interrupted.write_text('yes'), sys.exit(0))); "
        "ready.write_text('yes'); time.sleep(60)"
    )
    parent_code = (
        "import pathlib,signal,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]]); "
        "pathlib.Path(sys.argv[4]).write_text(str(child.pid)); "
        "signal.signal(signal.SIGINT, lambda *_: (child.wait(timeout=2), sys.exit(0))); "
        "time.sleep(60)"
    )
    command = [sys.executable, "-c", parent_code, child_code,
               str(child_ready), str(child_interrupted), str(child_pid)]
    return command, child_ready, child_interrupted, child_pid


def wait_for_file(path, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.01)
    raise TimeoutError(f"file was not created: {path}")


def assert_pid_exited(pid):
    with pytest.raises(ProcessLookupError):
        os.getpgid(pid)


def fake_perf_command(tmp_path, mode):
    fake_perf = tmp_path / "perf"
    wrapper_interrupted = tmp_path / f"{mode}.wrapper-interrupted"
    artifact = tmp_path / (f"{mode}.data" if mode == "record" else f"{mode}.csv")
    stats = tmp_path / f"{mode}.receiver.json"
    ready = tmp_path / f"{mode}.receiver.ready"
    fake_perf.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, signal, subprocess, sys\n"
        "separator = sys.argv.index('--')\n"
        "artifact = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "marker = pathlib.Path(sys.argv[sys.argv.index('--wrapper-marker') + 1])\n"
        "child = subprocess.Popen(sys.argv[separator + 1:])\n"
        "def interrupted(*_):\n"
        "    marker.write_text('wrapper received SIGINT')\n"
        "    child.terminate()\n"
        "signal.signal(signal.SIGINT, interrupted)\n"
        "status = child.wait()\n"
        "artifact.write_bytes(b'finalized profile artifact')\n"
        "sys.exit(status if status >= 0 else 128 - status)\n",
        encoding="utf-8")
    fake_perf.chmod(0o755)
    receiver_code = (
        "import pathlib,signal,sys,time; stats=pathlib.Path(sys.argv[1]); "
        "signal.signal(signal.SIGINT, lambda *_: "
        "(time.sleep(.1), stats.write_text('{\\\"status\\\": \\\"clean\\\"}'), sys.exit(0))); "
        "pathlib.Path(sys.argv[2]).write_text('ready'); time.sleep(60)"
    )
    command = [str(fake_perf), mode, "-o", str(artifact),
               "--wrapper-marker", str(wrapper_interrupted), "--",
               sys.executable, "-c", receiver_code, str(stats), str(ready)]
    return command, 7, wrapper_interrupted, artifact, stats, ready


@pytest.mark.parametrize("node_kind", ["local", "remote"])
@pytest.mark.parametrize("mode", ["stat", "record"])
def test_profile_shutdown_targets_receiver_and_finalizes_wrapper(
        tmp_path, node_kind, mode):
    if node_kind == "local":
        node = LocalNode()
    else:
        node = object.__new__(RemoteNode)

        def local_shell_exec(command, timeout=30.0):
            result = subprocess.run(["sh", "-c", command], check=True,
                                    capture_output=True, text=True, timeout=timeout)
            return result.stdout.strip()

        node._exec = local_shell_exec
    command, workload_index, wrapper_interrupted, artifact, stats, ready = fake_perf_command(
        tmp_path, mode)
    log_path = str(tmp_path / f"{node_kind}-{mode}.log")
    handle = node.start_process(command, log_path, workload_index=workload_index)
    try:
        assert handle.wrapper_pid != handle.workload_pid
        wait_for_file(ready)
        status = shutdown_process(node, handle, 2.0)
        assert status == 0
        assert json.loads(stats.read_text()) == {"status": "clean"}
        assert artifact.stat().st_size > 0
        assert not wrapper_interrupted.exists()
        assert not node.process_exists(handle.wrapper_pid)
        assert not node.process_exists(handle.workload_pid)
        if node_kind == "remote":
            assert Path(f"{log_path}.status").read_text() == "0"
    finally:
        if node.process_exists(handle.wrapper_pid):
            node.signal_wrapper(handle, signal.SIGKILL)
        for suffix in ("", ".status", ".pid", ".workload.pid"):
            Path(f"{log_path}{suffix}").unlink(missing_ok=True)


@pytest.mark.skipif(shutil.which("perf") is None, reason="perf is unavailable")
@pytest.mark.parametrize("mode", ["stat", "record"])
def test_real_perf_profile_shutdown_when_kernel_permissions_allow(tmp_path, mode):
    perf = shutil.which("perf")
    assert perf is not None
    artifact = tmp_path / ("perf.data" if mode == "record" else "perf.csv")
    stats = tmp_path / "receiver.json"
    receiver_code = """
import json
import pathlib
import signal
import sys

stats = pathlib.Path(sys.argv[1])
def stop(*_):
    stats.write_text(json.dumps({"status": "clean"}))
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
value = 1
while True:
    value = (value * 1103515245 + 12345) & 0x7fffffff
"""
    if mode == "stat":
        command = [perf, "stat", "-x,", "-o", str(artifact), "-e", "task-clock",
                   "--", sys.executable, "-c", receiver_code, str(stats)]
    else:
        command = [perf, "record", "-o", str(artifact), "-F", "99", "-g",
                   "--call-graph", "fp", "--", sys.executable, "-c", receiver_code,
                   str(stats)]
    workload_index = command.index(sys.executable)
    log_path = tmp_path / f"real-perf-{mode}.log"
    node = LocalNode()
    try:
        handle = node.start_process(command, str(log_path), workload_index=workload_index)
    except RuntimeError:
        log = log_path.read_text(errors="replace") if log_path.exists() else ""
        if "permission" in log.lower() or "not supported" in log.lower():
            pytest.skip(log.strip() or "perf events are not permitted")
        raise
    try:
        time.sleep(0.1)
        status = shutdown_process(node, handle, 5.0)
        log = log_path.read_text(errors="replace") if log_path.exists() else ""
        if status != 0 and ("permission" in log.lower() or "not supported" in log.lower()):
            pytest.skip(log.strip())
        assert status == 0, log
        assert json.loads(stats.read_text()) == {"status": "clean"}
        assert artifact.stat().st_size > 0
        if mode == "record":
            report = subprocess.run([perf, "report", "--stdio", "-i", str(artifact)],
                                    capture_output=True, text=True, timeout=30)
            assert report.returncode == 0, report.stderr
            assert (report.stdout + report.stderr).strip()
        assert not node.process_exists(handle.wrapper_pid)
        assert not node.process_exists(handle.workload_pid)
    finally:
        if node.process_exists(handle.workload_pid):
            os.kill(handle.workload_pid, signal.SIGKILL)
        if node.process_exists(handle.wrapper_pid):
            os.kill(handle.wrapper_pid, signal.SIGKILL)


def test_local_process_group_shutdown_reaches_descendants(tmp_path):
    node = LocalNode()
    command, child_ready, child_interrupted, child_pid_path = process_tree_command(tmp_path)
    handle = node.start_process(command, str(tmp_path / "local-process.log"))
    try:
        assert os.getpgid(handle.pid) == handle.pid
        wait_for_file(child_ready)
        child_pid = int(child_pid_path.read_text())
        assert os.getpgid(child_pid) == handle.pid

        node.signal_process(handle, signal.SIGINT)

        assert node.wait_process(handle, 2.0) == 0
        assert child_interrupted.read_text() == "yes"
        assert_pid_exited(child_pid)
    finally:
        if handle.process is not None and handle.process.poll() is None:
            os.killpg(handle.pid, signal.SIGKILL)
            handle.process.wait()


def test_remote_process_group_shutdown_writes_status_and_reaps_descendants(tmp_path):
    remote = object.__new__(RemoteNode)

    def local_shell_exec(command, timeout=30.0):
        result = subprocess.run(["sh", "-c", command], check=True,
                                capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()

    remote._exec = local_shell_exec
    command, child_ready, child_interrupted, child_pid_path = process_tree_command(tmp_path)
    log_path = str(tmp_path / "remote-tree.log")
    handle = remote.start_process(command, log_path)
    try:
        wait_for_file(child_ready)
        child_pid = int(child_pid_path.read_text())
        assert os.getpgid(handle.pid) == handle.pid
        assert os.getpgid(child_pid) == handle.pid

        remote.signal_process(handle, signal.SIGINT)

        assert remote.wait_process(handle, 2.0) == 0
        assert Path(f"{log_path}.status").read_text() == "0"
        assert child_interrupted.read_text() == "yes"
        assert_pid_exited(child_pid)
    finally:
        remote.signal_process(handle, signal.SIGTERM)
        remote.remove_files([log_path, f"{log_path}.status", f"{log_path}.pid"])


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
        self.wrapper_signals = []
        self.exited_pids = set()

    def run(self, command, timeout=30.0):
        return "mock perf report" if command[:2] == ["perf", "report"] else ""

    def start_process(self, command, log_path, workload_index=None):
        workload_pid = self.next_pid + 10_000 if workload_index is not None else None
        handle = ProcessHandle(self.next_pid, list(command), log_path,
                               workload_pid=workload_pid)
        self.next_pid += 1; self.handles.append(handle); return handle

    def wait_process(self, handle, timeout):
        self.exited_pids.add(handle.wrapper_pid)
        if handle.workload_pid is not None:
            self.exited_pids.add(handle.workload_pid)
        if any("receiver_" in part for part in handle.command):
            return self.receiver_status
        return self.sender_status

    def signal_process(self, handle, signum):
        self.signals.append((handle.shutdown_target_pid, signum))

    def signal_wrapper(self, handle, signum):
        self.wrapper_signals.append((handle.wrapper_pid, signum))

    def process_exists(self, pid):
        known = any(pid in {handle.wrapper_pid, handle.workload_pid}
                    for handle in self.handles)
        return known and pid not in self.exited_pids

    def wait_pid_exit(self, pid, timeout):
        return not self.process_exists(pid)

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


def test_fake_controller_pid_lifecycle_repetitions_metadata_and_progress(
        monkeypatch, tmp_path, capsys):
    fake = FakeNode(metadata_root=tmp_path)
    monkeypatch.setattr("main.get_node", lambda host, user: fake)
    monkeypatch.setattr("main.time.sleep", lambda seconds: None)
    config_path = tmp_path / "config.yaml"
    config = one_benchmark_config(tmp_path)
    config_path.write_text(yaml.safe_dump(config))
    session = run_campaign(config, config_path, skip_build=True)
    output = capsys.readouterr().out
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
    assert "preparing 0 qualification runs and up to 2 comparison runs" in output
    assert "START 1/2: comparison baseline_debug" in output
    assert "timed interval will be quiet for" in output
    assert "DONE 2/2: comparison baseline_debug" in output
    assert "ETA=00:00:00" in output
    assert "all 2 runs complete; generating summaries" in output
    assert len(fake.cleanup_requests) == 4
    assert all(fake.metadata_existed_at_cleanup)
    ordinary_suffixes = {
        ".bin", "_rx.json", "_receiver.log", "_receiver.log.status",
        "_receiver.log.pid", "_tx.json", "_sender.log", "_sender.log.status",
        "_sender.log.pid",
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

    session = run_campaign(config, config_path, skip_build=True)

    assert len(fake.cleanup_requests) == 2
    assert any(path.endswith(profile_suffix) for path in fake.cleanup_requests[0])
    assert all(fake.metadata_existed_at_cleanup)
    metadata = json.loads(next(session.glob("**/*_meta.json")).read_text())
    receiver_handle = next(handle for handle in fake.handles
                           if any("receiver_" in part for part in handle.command))
    assert metadata["processes"]["receiver_pid"] == receiver_handle.workload_pid
    assert metadata["processes"]["profile_wrapper_pid"] == receiver_handle.wrapper_pid
    assert (receiver_handle.workload_pid, signal.SIGINT) in fake.signals


def test_profile_finally_cleanup_signals_receiver_not_wrapper(monkeypatch, tmp_path):
    fake = FakeNode(sender_status=1)
    monkeypatch.setattr("main.get_node", lambda host, user: fake)
    monkeypatch.setattr("main.time.sleep", lambda seconds: None)
    config = one_benchmark_config(tmp_path)
    config["global"]["runtime"]["repetitions"] = 1
    config["benchmarks"][0]["profile"] = "record"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))

    with pytest.raises(RuntimeError, match="Sender failed"):
        run_campaign(config, config_path, skip_build=True)

    receiver_handle = next(handle for handle in fake.handles
                           if any("receiver_" in part for part in handle.command))
    assert fake.signals == [(receiver_handle.workload_pid, signal.SIGINT)]
    assert fake.wrapper_signals == []


def test_profile_shutdown_escalates_workload_before_wrapper():
    class StuckNode:
        def __init__(self):
            self.signals = []
            self.wrapper_signals = []
            self.waits = 0
            self.workload_waits = 0

        def signal_process(self, handle, signum):
            self.signals.append((handle.shutdown_target_pid, signum))

        def signal_wrapper(self, handle, signum):
            self.wrapper_signals.append((handle.wrapper_pid, signum))

        def wait_process(self, handle, timeout):
            self.waits += 1
            if self.waits < 4:
                raise subprocess.TimeoutExpired(handle.command, timeout)
            return 143

        def wait_pid_exit(self, pid, timeout):
            self.workload_waits += 1
            return self.workload_waits >= 3

        def process_exists(self, pid):
            return False

    node = StuckNode()
    handle = ProcessHandle(200, ["perf", "record"], "/tmp/profile.log",
                           workload_pid=201)

    assert shutdown_process(node, handle, 0.1) == 143
    assert node.signals == [
        (201, signal.SIGINT),
        (201, signal.SIGTERM),
        (201, signal.SIGKILL),
    ]
    assert node.wrapper_signals == [(200, signal.SIGTERM)]


def test_profile_shutdown_reaps_workload_even_if_wrapper_already_exited():
    class DetachedWorkloadNode:
        def __init__(self):
            self.alive = True
            self.signals = []

        def signal_process(self, handle, signum):
            self.signals.append((handle.shutdown_target_pid, signum))
            if signum == signal.SIGKILL:
                self.alive = False

        def signal_wrapper(self, handle, signum):
            raise AssertionError("exited wrapper must not be signaled")

        def wait_process(self, handle, timeout):
            return 0

        def wait_pid_exit(self, pid, timeout):
            return not self.alive

        def process_exists(self, pid):
            return self.alive

    node = DetachedWorkloadNode()
    handle = ProcessHandle(300, ["perf", "record"], "/tmp/profile.log",
                           workload_pid=301)

    assert shutdown_process(node, handle, 0.1) == 0
    assert node.signals == [
        (301, signal.SIGINT),
        (301, signal.SIGTERM),
        (301, signal.SIGKILL),
    ]
    assert not node.alive


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
