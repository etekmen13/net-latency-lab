"""Auditable two-node UDP benchmark orchestrator.

Distributed timed intervals are deliberately control-plane silent: all SSH
setup occurs before the sender starts and status/counter collection occurs only
after the declared sender duration plus drain interval has elapsed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml

try:
    import paramiko
    from scp import SCPClient
except ImportError:
    paramiko = None
    SCPClient = None

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
RECEIVER_BINARIES = {"receiver_baseline", "receiver_batched", "receiver_threaded"}


def progress(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] {message}", flush=True)


def format_duration(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@dataclass
class ProcessHandle:
    pid: int
    command: list[str]
    log_path: str
    process: subprocess.Popen[str] | None = None
    log_stream: Any = None
    monitor_pid: int | None = None


@dataclass(frozen=True)
class RunTuple:
    benchmark_index: int
    benchmark_name: str
    campaign: str
    repetition: int
    rate: int
    burst: int
    batch: int


class NodeController:
    def run(self, command: Sequence[str], timeout: float = 30.0) -> str:
        raise NotImplementedError

    def start_process(self, command: Sequence[str], log_path: str) -> ProcessHandle:
        raise NotImplementedError

    def wait_process(self, handle: ProcessHandle, timeout: float) -> int:
        raise NotImplementedError

    def signal_process(self, handle: ProcessHandle, signum: int) -> None:
        raise NotImplementedError

    def fetch_file(self, source: str, destination: Path) -> None:
        raise NotImplementedError

    def remove_files(self, paths: Sequence[str]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def read_counter(self, command: Sequence[str], parser) -> dict[str, Any]:
        try:
            return {"value": int(parser(self.run(command))), "error": None}
        except Exception as exc:
            return {"value": None, "error": str(exc)}

    def snapshot_counters(self, interface: str) -> dict[str, dict[str, Any]]:
        counters = {"udp_rcvbuf_errors": self.read_counter(
            ["cat", "/proc/net/snmp"], parse_udp_rcvbuf_errors)}
        for name in ("rx_dropped", "rx_errors", "rx_missed_errors"):
            path = f"/sys/class/net/{interface}/statistics/{name}"
            counters[f"nic_{name}"] = self.read_counter(
                ["cat", path], lambda value: value.strip())
        return counters


class LocalNode(NodeController):
    def run(self, command: Sequence[str], timeout: float = 30.0) -> str:
        result = subprocess.run(list(command), check=True, capture_output=True,
                                text=True, timeout=timeout)
        return result.stdout.strip()

    def start_process(self, command: Sequence[str], log_path: str) -> ProcessHandle:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(list(command), stdout=stream,
                                   stderr=subprocess.STDOUT, text=True)
        return ProcessHandle(process.pid, list(command), log_path, process, stream)

    def wait_process(self, handle: ProcessHandle, timeout: float) -> int:
        assert handle.process is not None
        try:
            return handle.process.wait(timeout=timeout)
        finally:
            if handle.process.poll() is not None and handle.log_stream is not None:
                handle.log_stream.close()
                handle.log_stream = None

    def signal_process(self, handle: ProcessHandle, signum: int) -> None:
        assert handle.process is not None
        if handle.process.poll() is None:
            handle.process.send_signal(signum)

    def fetch_file(self, source: str, destination: Path) -> None:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source_path.read_bytes())

    def remove_files(self, paths: Sequence[str]) -> None:
        for path in paths:
            Path(path).unlink(missing_ok=True)


class RemoteNode(NodeController):
    def __init__(self, host: str, user: str):
        if paramiko is None or SCPClient is None:
            raise RuntimeError("Distributed mode requires paramiko and scp")
        self.host = host
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.client.connect(host, username=user, timeout=10, allow_agent=True,
                            look_for_keys=True)

    def _exec(self, command: str, timeout: float = 30.0) -> str:
        _stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        status = stdout.channel.recv_exit_status()
        output, error = stdout.read().decode(), stderr.read().decode()
        if status != 0:
            raise RuntimeError(f"Remote command failed ({status}): {error.strip()}")
        return output.strip()

    def run(self, command: Sequence[str], timeout: float = 30.0) -> str:
        return self._exec(shlex.join(command), timeout)

    def start_process(self, command: Sequence[str], log_path: str) -> ProcessHandle:
        status_path = f"{log_path}.status"
        pid_path = f"{log_path}.pid"
        inner = (
            f"{shlex.join(command)} & child=$!; "
            f"printf '%s' \"$child\" > {shlex.quote(pid_path)}; "
            "wait \"$child\"; code=$?; "
            f"printf '%s' \"$code\" > {shlex.quote(status_path)}; exit \"$code\"")
        launch = (f"nohup sh -c {shlex.quote(inner)} > {shlex.quote(log_path)} "
                  f"2>&1 < /dev/null & monitor=$!; "
                  f"while test ! -s {shlex.quote(pid_path)}; do "
                  "if ! kill -0 \"$monitor\" 2>/dev/null; then wait \"$monitor\"; exit $?; fi; "
                  "sleep 0.01; done; "
                  f"printf '%s %s\n' \"$monitor\" \"$(cat {shlex.quote(pid_path)})\"")
        output = self._exec(launch).split()
        if len(output) != 2:
            raise RuntimeError(f"Remote process launch returned invalid PIDs: {' '.join(output)}")
        monitor_pid, child_pid = map(int, output)
        return ProcessHandle(child_pid, list(command), log_path,
                             monitor_pid=monitor_pid)

    def wait_process(self, handle: ProcessHandle, timeout: float) -> int:
        # This is invoked only after the orchestrator's no-control-traffic timed
        # sleep. Polling here is outside the measured interval.
        status_path = f"{handle.log_path}.status"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            monitor_check = (f"elif kill -0 {handle.monitor_pid} 2>/dev/null; then echo running; "
                             if handle.monitor_pid is not None else "")
            output = self._exec(
                f"if test -f {shlex.quote(status_path)}; then cat {shlex.quote(status_path)}; "
                f"elif kill -0 {handle.pid} 2>/dev/null; then echo running; "
                f"{monitor_check}"
                "else echo exited; fi")
            if output != "running":
                if output == "exited":
                    raise RuntimeError(f"Remote PID {handle.pid} exited without status")
                return int(output)
            time.sleep(0.05)
        raise subprocess.TimeoutExpired(handle.command, timeout)

    def signal_process(self, handle: ProcessHandle, signum: int) -> None:
        name = signal.Signals(signum).name.removeprefix("SIG")
        self._exec(f"kill -{name} {handle.pid} 2>/dev/null || true")

    def fetch_file(self, source: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with SCPClient(self.client.get_transport()) as client:
            client.get(source, str(destination))

    def remove_files(self, paths: Sequence[str]) -> None:
        if paths:
            self._exec("rm -f -- " + " ".join(shlex.quote(path) for path in paths))

    def close(self) -> None:
        self.client.close()


def parse_udp_rcvbuf_errors(text: str) -> int:
    lines = [line.split() for line in text.splitlines() if line.startswith("Udp:")]
    if len(lines) < 2:
        raise ValueError("/proc/net/snmp has no UDP header/value pair")
    headers, values = lines[-2], lines[-1]
    if "RcvbufErrors" not in headers:
        raise ValueError("UDP RcvbufErrors is unavailable")
    return int(values[headers.index("RcvbufErrors")])


def counter_deltas(before: dict[str, dict[str, Any]],
                   after: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(set(before) | set(after)):
        first = before.get(name, {"value": None, "error": "missing before sample"})
        second = after.get(name, {"value": None, "error": "missing after sample"})
        error = first.get("error") or second.get("error")
        delta = (None if error or first.get("value") is None or second.get("value") is None
                 else second["value"] - first["value"])
        result[name] = {"before": first.get("value"), "after": second.get("value"),
                        "delta": delta, "error": error}
    return result


def is_local(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def management_host(node: dict[str, Any]) -> str:
    return str(node.get("management_host", node.get("host", "")))


def benchmark_ip(node: dict[str, Any]) -> str:
    return str(node.get("benchmark_ip", node.get("host", "")))


def get_node(host: str, user: str) -> NodeController:
    return LocalNode() if is_local(host) else RemoteNode(host, user)


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict) or not isinstance(config.get("global"), dict):
        raise ValueError("Configuration requires a global object")
    global_config = config["global"]
    required = {"topology", "remote_project_root", "local_data_dir", "user",
                "nodes", "runtime", "metadata_collection", "tuning_state"}
    missing = required - global_config.keys()
    if missing:
        raise ValueError(f"global is missing: {', '.join(sorted(missing))}")
    topology, nodes = global_config["topology"], global_config["nodes"]
    if topology not in {"local_loopback", "distributed_ethernet"}:
        raise ValueError("global.topology must be local_loopback or distributed_ethernet")
    for role in ("receiver", "sender"):
        node = nodes.get(role)
        if not isinstance(node, dict) or not management_host(node) or not benchmark_ip(node):
            raise ValueError(f"global.nodes.{role} requires management_host and benchmark_ip")
    rx_management, tx_management = map(management_host, (nodes["receiver"], nodes["sender"]))
    rx_benchmark, tx_benchmark = map(benchmark_ip, (nodes["receiver"], nodes["sender"]))
    interface = nodes["receiver"].get("interface")
    if not interface or not re.fullmatch(r"[A-Za-z0-9_.:-]+", interface):
        raise ValueError("receiver interface is missing or invalid")
    if topology == "local_loopback":
        if not all(is_local(value) for value in (rx_management, tx_management,
                                                  rx_benchmark, tx_benchmark)) or interface != "lo":
            raise ValueError("local_loopback requires loopback control/data addresses and interface lo")
    elif (is_local(rx_management) or is_local(tx_management) or
          rx_management == tx_management or rx_benchmark == tx_benchmark or interface == "lo"):
        raise ValueError("distributed_ethernet requires distinct management hosts and benchmark IPs")
    runtime = global_config["runtime"]
    runtime_required = {"repetitions", "port", "receiver_startup_seconds",
                        "shutdown_timeout_seconds", "scheduler", "priority"}
    missing_runtime = runtime_required - runtime.keys()
    if missing_runtime:
        raise ValueError(f"global.runtime is missing: {', '.join(sorted(missing_runtime))}")
    if not isinstance(runtime["repetitions"], int) or runtime["repetitions"] < 1:
        raise ValueError("repetitions must be a positive integer")
    if not isinstance(runtime["port"], int) or not 1 <= runtime["port"] <= 65535:
        raise ValueError("port must be 1..65535")
    for delay_name in ("receiver_startup_seconds", "shutdown_timeout_seconds"):
        if not isinstance(runtime[delay_name], (int, float)) or runtime[delay_name] <= 0:
            raise ValueError(f"{delay_name} must be positive")
    drain = runtime.get("post_sender_drain_seconds", 1.0)
    if not isinstance(drain, (int, float)) or drain < 0:
        raise ValueError("post_sender_drain_seconds must be nonnegative")
    for cpu_name in ("receiver_cpu", "worker_cpu", "sender_cpu"):
        cpu = runtime.get(cpu_name)
        if cpu is not None and (not isinstance(cpu, int) or cpu < 0):
            raise ValueError(f"{cpu_name} must be null or a nonnegative integer")
    if runtime["scheduler"] not in {"other", "fifo", "rr"}:
        raise ValueError("scheduler must be other, fifo, or rr")
    priority = runtime["priority"]
    if ((runtime["scheduler"] == "other" and priority != 0) or
        (runtime["scheduler"] != "other" and not 1 <= priority <= 99)):
        raise ValueError("scheduler priority is inconsistent with policy")
    benchmarks = config.get("benchmarks")
    if not isinstance(benchmarks, list) or not benchmarks:
        raise ValueError("At least one benchmark is required")
    names: set[str] = set()
    for benchmark in benchmarks:
        if not benchmark.get("enabled", True):
            continue
        name = benchmark.get("name")
        if not name or name in names or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Benchmark names must be unique safe identifiers")
        names.add(name)
        receiver, sender = benchmark.get("receiver", {}), benchmark.get("sender", {})
        binary = receiver.get("binary")
        if binary not in RECEIVER_BINARIES:
            raise ValueError(f"Unknown receiver binary: {binary}")
        batches = receiver.get("batch_sizes", [1])
        if (not isinstance(batches, list) or not batches or
                any(not isinstance(value, int) or not 1 <= value <= 1024 for value in batches)):
            raise ValueError(f"{name}: batch_sizes must contain integers 1..1024")
        if binary == "receiver_baseline" and batches != [1]:
            raise ValueError(f"{name}: baseline only supports batch size 1")
        for field in ("work_ns", "sample_every"):
            value = receiver.get(field, 1 if field == "sample_every" else 0)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name}: receiver.{field} must be a nonnegative integer")
        for field in ("mode", "duration_seconds", "rates_pps", "payload_size"):
            if field not in sender:
                raise ValueError(f"{name}: sender.{field} is required")
        if sender["mode"] not in {"steady", "burst"} or float(sender["duration_seconds"]) <= 0:
            raise ValueError(f"{name}: invalid sender mode or duration")
        if (not isinstance(sender["rates_pps"], list) or not sender["rates_pps"] or
                any(not isinstance(rate, int) or rate <= 0 for rate in sender["rates_pps"])):
            raise ValueError(f"{name}: rates_pps must be positive integers")
        if not isinstance(sender["payload_size"], int) or not 16 <= sender["payload_size"] <= 65507:
            raise ValueError(f"{name}: payload_size must be 16..65507")
        bursts = sender.get("burst_sizes", [1])
        if sender["mode"] == "steady" and bursts != [1]:
            raise ValueError(f"{name}: steady mode requires burst_sizes: [1]")
    return config


def binary_dir(project_root: str, runtime: dict[str, Any]) -> Path:
    return Path(project_root) / "build" / str(runtime.get("build_subdir", ""))


def receiver_command(project_root: str, benchmark: dict[str, Any], runtime: dict[str, Any],
                     output: str, stats: str, batch: int) -> list[str]:
    receiver = benchmark["receiver"]
    command = [str(binary_dir(project_root, runtime) / receiver["binary"]),
               "--output", output, "--stats", stats, "--port", str(runtime["port"]),
               "--max-packets", "0", "--scheduler", runtime["scheduler"],
               "--priority", str(runtime["priority"]),
               "--work", str(receiver.get("work_ns", 0)),
               "--sample-every", str(receiver.get("sample_every", 1)),
               "--socket-buffer", str(runtime.get("receive_buffer_bytes", 0))]
    if runtime.get("receiver_cpu") is not None:
        command += ["--cpu", str(runtime["receiver_cpu"])]
    if receiver["binary"] in {"receiver_batched", "receiver_threaded"}:
        command += ["--batch", str(batch)]
    if receiver["binary"] == "receiver_threaded" and runtime.get("worker_cpu") is not None:
        command += ["--worker-cpu", str(runtime["worker_cpu"])]
    return list(global_prefix(runtime)) + command


def sender_command(project_root: str, receiver_host: str, benchmark: dict[str, Any],
                   runtime: dict[str, Any], stats: str, rate: int, burst: int) -> list[str]:
    sender = benchmark["sender"]
    command = [str(binary_dir(project_root, runtime) / "sender"), "--ip", receiver_host,
               "--port", str(runtime["port"]), "--rate", str(rate),
               "--duration", str(sender["duration_seconds"]), "--mode", sender["mode"],
               "--burst", str(burst), "--payload-size", str(sender["payload_size"]),
               "--timestamp-every", str(benchmark["receiver"].get("sample_every", 1)),
               "--socket-buffer", str(runtime.get("send_buffer_bytes", 0)), "--stats", stats]
    if runtime.get("sender_cpu") is not None:
        command += ["--cpu", str(runtime["sender_cpu"])]
    return list(global_prefix(runtime)) + command


def global_prefix(runtime: dict[str, Any]) -> list[str]:
    prefix = runtime.get("privilege_prefix", [])
    if not isinstance(prefix, list) or any(not isinstance(part, str) for part in prefix):
        raise ValueError("runtime.privilege_prefix must be a string list")
    return prefix


def safe_read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def probe(node: NodeController, command: Sequence[str]) -> dict[str, Any]:
    try:
        return {"value": node.run(command), "error": None}
    except Exception as exc:
        return {"value": None, "error": str(exc)}


def collect_machine_metadata(node: NodeController, project_root: str,
                             interface: str | None) -> dict[str, Any]:
    commands: dict[str, list[str]] = {
        "hostname": ["hostname"], "kernel": ["uname", "-a"], "cpu": ["lscpu"],
        "compiler": ["c++", "--version"], "cmake": ["cmake", "--version"],
        "git_commit": ["git", "-C", project_root, "rev-parse", "HEAD"],
        "git_status": ["git", "-C", project_root, "status", "--porcelain"],
        "chrony_tracking": ["chronyc", "tracking"],
        "chrony_sources": ["chronyc", "sources", "-v"],
        "governor": ["sh", "-c", "for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do test -r \"$f\" && printf '%s=' \"$f\" && cat \"$f\"; done"],
        "clock_state": ["sh", "-c", "for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq; do test -r \"$f\" && printf '%s=' \"$f\" && cat \"$f\"; done"],
        "thermal": ["sh", "-c", "vcgencmd measure_temp 2>/dev/null || cat /sys/class/thermal/thermal_zone0/temp"],
        "throttled": ["sh", "-c", "vcgencmd get_throttled 2>/dev/null || echo unavailable"],
        "socket_maxima": ["sysctl", "net.core.rmem_max", "net.core.wmem_max"],
    }
    if interface:
        commands.update({
            "interface": ["ip", "-details", "link", "show", "dev", interface],
            "driver": ["ethtool", "-i", interface],
            "link": ["ethtool", interface],
            "offloads": ["ethtool", "-k", interface],
            "irq_lines": ["sh", "-c", f"grep -F {shlex.quote(interface)} /proc/interrupts || true"],
            "irq_affinity": ["sh", "-c", "for f in /proc/irq/*/smp_affinity_list; do test -r \"$f\" && printf '%s=' \"$f\" && cat \"$f\"; done"],
        })
    metadata = {name: probe(node, command) for name, command in commands.items()}
    metadata["git_dirty"] = {
        "value": bool(metadata["git_status"]["value"]) if metadata["git_status"]["error"] is None else None,
        "error": metadata["git_status"]["error"],
    }
    return metadata


def run_state(node: NodeController, interface: str) -> dict[str, Any]:
    return {name: probe(node, command) for name, command in {
        "thermal": ["sh", "-c", "vcgencmd measure_temp 2>/dev/null || cat /sys/class/thermal/thermal_zone0/temp"],
        "throttled": ["sh", "-c", "vcgencmd get_throttled 2>/dev/null || echo unavailable"],
        "link": ["ethtool", interface],
        "offloads": ["ethtool", "-k", interface],
        "clock_state": ["sh", "-c", "for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq; do test -r \"$f\" && printf '%s=' \"$f\" && cat \"$f\"; done"],
    }.items()}


def build_nodes(nodes: list[NodeController], project_root: str,
                runtime: dict[str, Any]) -> None:
    preset = str(runtime.get("build_preset", "dev"))
    for node in dict.fromkeys(nodes):
        node.run(["cmake", "--preset", preset, "-S", project_root], timeout=120)
        node.run(["cmake", "--build", str(binary_dir(project_root, runtime)), "-j2"], timeout=240)


def make_run_tuples(config: dict[str, Any]) -> tuple[list[RunTuple], list[RunTuple]]:
    runtime = config["global"]["runtime"]
    qualification: list[RunTuple] = []
    comparative: list[RunTuple] = []
    for index, benchmark in enumerate(config["benchmarks"]):
        if not benchmark.get("enabled", True):
            continue
        campaign = str(benchmark.get("campaign", "comparison"))
        repetitions = int(benchmark.get("repetitions", runtime["repetitions"]))
        destination = qualification if campaign == "sender_qualification" else comparative
        for repetition in range(1, repetitions + 1):
            for rate in benchmark["sender"]["rates_pps"]:
                for burst in benchmark["sender"].get("burst_sizes", [1]):
                    for batch in benchmark["receiver"].get("batch_sizes", [1]):
                        destination.append(RunTuple(index, benchmark["name"], campaign,
                                                    repetition, rate, burst, batch))
    seed = int(runtime.get("shuffle_seed", 477))
    random.Random(seed).shuffle(qualification)
    random.Random(seed).shuffle(comparative)
    return qualification, comparative


def qualification_rates(run_metadata: list[dict[str, Any]]) -> list[int]:
    by_rate: dict[int, list[dict[str, Any]]] = {}
    for metadata in run_metadata:
        if metadata["run"]["campaign"] == "sender_qualification":
            by_rate.setdefault(int(metadata["run"]["requested_rate_pps"]), []).append(metadata)
    eligible = []
    for rate, runs in sorted(by_rate.items()):
        if runs and all(int(run["sender_stats"]["failed_sends"]) == 0 and
                        float(run["sender_stats"]["achieved_successful_send_pps"]) >= .99 * rate
                        for run in runs):
            eligible.append(rate)
    return eligible


def throttled(state: dict[str, Any]) -> bool:
    value = str(state.get("throttled", {}).get("value", ""))
    match = re.search(r"0x([0-9a-fA-F]+)", value)
    return bool(match and int(match.group(1), 16) != 0)


def validity_reasons(sender: dict[str, Any], receiver: dict[str, Any],
                     counters: dict[str, Any], before_state: dict[str, Any],
                     after_state: dict[str, Any], topology: str,
                     sender_before_state: dict[str, Any] | None = None,
                     sender_after_state: dict[str, Any] | None = None) -> list[str]:
    reasons: list[str] = []
    successful = int(sender["successful_sends"])
    unique_received = int(receiver["unique_valid_packets"])
    unique_processed = int(receiver["unique_processed_packets"])
    if unique_received > successful:
        reasons.append("unique receives exceed successful submissions")
    if unique_processed > successful:
        reasons.append("unique processed exceeds successful submissions")
    if int(receiver["processed_packets"]) + int(receiver["spsc_overflow"]) != int(receiver["valid_packets"]):
        reasons.append("SPSC/processing accounting does not reconcile")
    for name in ("receiver_affinity", "receiver_scheduler", "worker_affinity", "worker_scheduler"):
        outcome = receiver.get(name)
        if outcome is not None and not outcome.get("success", False):
            reasons.append(f"{name} was not applied")
    if not sender.get("cpu_affinity", {}).get("success", False):
        reasons.append("sender affinity was not applied")
    for side, stats in (("receive", receiver), ("send", sender)):
        requested = int(stats.get("requested_socket_buffer_bytes", 0))
        observed = int(stats.get("observed_socket_buffer_bytes", -1))
        if requested > 0 and observed < requested:
            reasons.append(f"observed {side} socket buffer is below the request")
    if int(receiver["valid_packets"]) and (
            int(receiver.get("first_receive_mono_ns", 0)) == 0 or
            int(receiver.get("last_receive_mono_ns", 0)) == 0):
        reasons.append("receive monotonic window is missing")
    if int(receiver["processed_packets"]) and (
            int(receiver.get("first_processing_mono_ns", 0)) == 0 or
            int(receiver.get("last_processing_mono_ns", 0)) == 0):
        reasons.append("processing monotonic window is missing")
    if int(receiver.get("queue_depth_at_shutdown", 0)) != 0:
        reasons.append("application backlog remained after the post-sender drain")
    if int(receiver.get("socket_pending_bytes_at_shutdown", 0)) != 0:
        reasons.append("kernel socket backlog remained after the post-sender drain")
    if any(value.get("delta") is not None and value["delta"] < 0 for value in counters.values()):
        reasons.append("negative kernel or NIC counter delta")
    if topology == "distributed_ethernet" and (throttled(before_state) or throttled(after_state)):
        reasons.append("thermal/power throttling flag was nonzero")
    if topology == "distributed_ethernet" and sender_before_state and sender_after_state and (
            throttled(sender_before_state) or throttled(sender_after_state)):
        reasons.append("sender thermal/power throttling flag was nonzero")
    if topology == "distributed_ethernet":
        for label, state in (("receiver before", before_state), ("receiver after", after_state),
                             ("sender before", sender_before_state or {}),
                             ("sender after", sender_after_state or {})):
            missing = sorted(name for name, result in state.items()
                             if isinstance(result, dict) and result.get("error"))
            if missing:
                reasons.append(f"{label} metadata unavailable: {', '.join(missing)}")
    return reasons


def execute_run(item: RunTuple, config: dict[str, Any], session_id: str,
                session_dir: Path, receiver_node: NodeController,
                sender_node: NodeController, environment: dict[str, Any]) -> dict[str, Any]:
    global_config, runtime = config["global"], config["global"]["runtime"]
    nodes, benchmark = global_config["nodes"], config["benchmarks"][item.benchmark_index]
    project_root = global_config["remote_project_root"]
    run_id = (f"{item.benchmark_name}_r{item.repetition:02d}_{item.rate}pps_"
              f"burst{item.burst}_batch{item.batch}")
    run_dir = session_dir / item.benchmark_name
    run_dir.mkdir(exist_ok=True)
    remote_base = f"/tmp/nll_{session_id}_{run_id}"
    remote_trace = remote_base + ".bin"
    remote_rx_stats, remote_tx_stats = remote_base + "_rx.json", remote_base + "_tx.json"
    receiver_log, sender_log = remote_base + "_receiver.log", remote_base + "_sender.log"
    receiver_artifacts = [remote_trace, remote_rx_stats, receiver_log,
                          f"{receiver_log}.status", f"{receiver_log}.pid"]
    sender_artifacts = [remote_tx_stats, sender_log, f"{sender_log}.status",
                        f"{sender_log}.pid"]
    rx_command = receiver_command(project_root, benchmark, runtime, remote_trace,
                                  remote_rx_stats, item.batch)
    tx_command = sender_command(project_root, benchmark_ip(nodes["receiver"]),
                                benchmark, runtime, remote_tx_stats,
                                item.rate, item.burst)
    interface = nodes["receiver"]["interface"]
    profile = benchmark.get("profile")
    remote_profile = remote_base + ("_perf_stat.csv" if profile == "stat" else "_perf.data")
    if profile in {"stat", "record"}:
        receiver_artifacts.append(remote_profile)
    unavailable_events: list[str] = []
    if profile == "stat":
        events = list(benchmark.get("perf_events", [
            "task-clock", "cycles", "instructions", "branches", "branch-misses",
            "cache-references", "cache-misses", "context-switches", "cpu-migrations",
            "page-faults", "syscalls:sys_enter_recvfrom", "syscalls:sys_enter_recvmmsg"]))
        perf_list = receiver_node.run(["perf", "list"], timeout=60)
        for event in list(events):
            if event.startswith("syscalls:") and event.split(":", 1)[1] not in perf_list:
                events.remove(event)
                unavailable_events.append(event)
        rx_command = ["perf", "stat", "-x,", "-o", remote_profile, "-e", ",".join(events), "--", *rx_command]
    elif profile == "record":
        rx_command = ["perf", "record", "-o", remote_profile, "-F", "99", "-g",
                      "--call-graph", "fp", "--", *rx_command]
    active_receiver: ProcessHandle | None = None
    active_sender: ProcessHandle | None = None
    cleanup_eligible = False
    try:
        active_receiver = receiver_node.start_process(rx_command, receiver_log)
        receiver_pid = active_receiver.pid
        time.sleep(float(runtime["receiver_startup_seconds"]))
        before_counters = receiver_node.snapshot_counters(interface)
        before_state = run_state(receiver_node, interface)
        sender_interface = nodes["sender"].get("interface", interface)
        sender_before_state = run_state(sender_node, sender_interface)
        active_sender = sender_node.start_process(tx_command, sender_log)
        sender_handle = active_sender

        # No controller calls occur in this interval.
        silence_seconds = (float(benchmark["sender"]["duration_seconds"]) +
                           float(runtime.get("post_sender_drain_seconds", 1.0)))
        time.sleep(silence_seconds)

        sender_status = sender_node.wait_process(sender_handle,
                                                 float(runtime["shutdown_timeout_seconds"]))
        active_sender = None
        if sender_status != 0:
            raise RuntimeError(f"Sender failed with status {sender_status}: PID {sender_handle.pid}")
        after_counters = receiver_node.snapshot_counters(interface)
        after_state = run_state(receiver_node, interface)
        sender_after_state = run_state(sender_node, sender_interface)
        receiver_node.signal_process(active_receiver, signal.SIGINT)
        receiver_status = receiver_node.wait_process(
            active_receiver, float(runtime["shutdown_timeout_seconds"]))
        if receiver_status != 0:
            raise RuntimeError(f"Receiver failed with status {receiver_status}: PID {active_receiver.pid}")
        active_receiver = None

        trace_path = run_dir / f"{run_id}.bin"
        rx_stats_path = run_dir / f"{run_id}_rx_stats.json"
        tx_stats_path = run_dir / f"{run_id}_tx_stats.json"
        receiver_node.fetch_file(remote_trace, trace_path)
        receiver_node.fetch_file(remote_rx_stats, rx_stats_path)
        sender_node.fetch_file(remote_tx_stats, tx_stats_path)
        receiver_stats, sender_stats = safe_read_json(rx_stats_path), safe_read_json(tx_stats_path)
        counters = counter_deltas(before_counters, after_counters)
        successful = int(sender_stats["successful_sends"])
        unique_received = int(receiver_stats["unique_valid_packets"])
        unique_processed = int(receiver_stats["unique_processed_packets"])
        elapsed = float(sender_stats["elapsed_seconds"])
        ingress_loss, application_loss = successful - unique_received, successful - unique_processed
        reasons = validity_reasons(sender_stats, receiver_stats, counters,
                                   before_state, after_state, global_config["topology"],
                                   sender_before_state, sender_after_state)
        metadata = {
            "schema_version": 2, "session_id": session_id, "run_id": run_id,
            "repetition_id": item.repetition, "topology": global_config["topology"],
            "run": {"campaign": item.campaign,
                    "receiver_variant": receiver_stats["variant"],
                    "requested_rate_pps": item.rate,
                    "duration_seconds": benchmark["sender"]["duration_seconds"],
                    "payload_size": benchmark["sender"]["payload_size"],
                    "mode": benchmark["sender"]["mode"], "burst_size": item.burst,
                    "batch_size": item.batch,
                    "work_ns": benchmark["receiver"].get("work_ns", 0),
                    "sample_every": benchmark["receiver"].get("sample_every", 1),
                    "post_sender_drain_seconds": runtime.get("post_sender_drain_seconds", 1.0)},
            "processes": {"receiver_pid": receiver_pid, "sender_pid": sender_handle.pid,
                          "receiver_command": rx_command, "sender_command": tx_command,
                          "control_plane_silent_seconds": silence_seconds},
            "sender_stats": sender_stats, "receiver_stats": receiver_stats,
            "packet_accounting": {
                "requested_rate_pps": item.rate,
                "offered_pps": successful / elapsed if elapsed > 0 else 0.0,
                "received_pps": unique_received / elapsed if elapsed > 0 else 0.0,
                "processed_pps": unique_processed / elapsed if elapsed > 0 else 0.0,
                "ingress_loss": ingress_loss,
                "application_loss": application_loss,
                "ingress_loss_pct": 100.0 * ingress_loss / successful if successful else 0.0,
                "application_loss_pct": 100.0 * application_loss / successful if successful else 0.0,
            },
            "counters": counters,
            "run_environment": {
                "receiver": {"before": before_state, "after": after_state},
                "sender": {"before": sender_before_state, "after": sender_after_state}},
            "validity": {"valid": not reasons, "reasons": reasons},
            "configured_tuning_state": global_config["tuning_state"],
            "environment": environment,
        }
        if profile == "stat":
            profile_path = run_dir / f"{run_id}_perf_stat.csv"
            receiver_node.fetch_file(remote_profile, profile_path)
            metadata["profile"] = {"mode": "stat", "artifact": profile_path.name,
                                   "unavailable_events": unavailable_events,
                                   "contributes_to_claim_measurements": False}
        elif profile == "record":
            report_text = receiver_node.run(["perf", "report", "--stdio", "-i", remote_profile], timeout=120)
            report_path = run_dir / f"{run_id}_perf_report.txt"
            report_path.write_text(report_text + "\n", encoding="utf-8")
            perf_data_path = run_dir / f"{run_id}.perf.data"
            receiver_node.fetch_file(remote_profile, perf_data_path)
            metadata["profile"] = {"mode": "record", "report": report_path.name,
                                   "perf_data": perf_data_path.name,
                                   "contributes_to_claim_measurements": False}
        metadata_path = run_dir / f"{run_id}_meta.json"
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        cleanup_eligible = True
        return {"run_id": run_id, "metadata": str(metadata_path.relative_to(session_dir)),
                "trace": str(trace_path.relative_to(session_dir)), "metadata_object": metadata}
    finally:
        if active_sender is not None:
            try:
                sender_node.signal_process(active_sender, signal.SIGINT)
                sender_node.wait_process(active_sender, float(runtime["shutdown_timeout_seconds"]))
            except Exception:
                pass
        if active_receiver is not None:
            try:
                receiver_node.signal_process(active_receiver, signal.SIGINT)
                receiver_node.wait_process(active_receiver, float(runtime["shutdown_timeout_seconds"]))
            except Exception:
                pass
        if cleanup_eligible:
            receiver_node.remove_files(receiver_artifacts)
            sender_node.remove_files(sender_artifacts)


def run_campaign(config: dict[str, Any], config_path: Path,
                 skip_build: bool = False) -> Path:
    validate_config(config)
    global_config, runtime, nodes = config["global"], config["global"]["runtime"], config["global"]["nodes"]
    rx_host, tx_host = management_host(nodes["receiver"]), management_host(nodes["sender"])
    receiver_node = get_node(rx_host, global_config["user"])
    sender_node = receiver_node if tx_host == rx_host else get_node(tx_host, global_config["user"])
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    local_root = Path(global_config.get("local_project_root", Path(__file__).parent))
    if str(local_root) == ".":
        local_root = Path(__file__).parent
    session_dir = (local_root / global_config["local_data_dir"] / f"session_{session_id}").resolve()
    session_dir.mkdir(parents=True)
    config_snapshot = session_dir / "config_snapshot.yaml"
    config_snapshot.write_bytes(config_path.read_bytes())
    qualification, comparative = make_run_tuples(config)
    progress(f"Session {session_id}: preparing {len(qualification)} qualification "
             f"runs and up to {len(comparative)} comparison runs")
    progress("Collecting frozen environment metadata from receiver, sender, and controller")
    environment = {
        "receiver": collect_machine_metadata(receiver_node, global_config["remote_project_root"],
                                             nodes["receiver"]["interface"]) if global_config["metadata_collection"] else {},
        "sender": collect_machine_metadata(sender_node, global_config["remote_project_root"],
                                           nodes["sender"].get("interface")) if global_config["metadata_collection"] else {},
        "orchestrator": collect_machine_metadata(LocalNode(), str(Path(__file__).parent), None) if global_config["metadata_collection"] else {},
    }
    progress("Environment metadata collection complete")
    manifest: dict[str, Any] = {
        "schema_version": 2, "session_id": session_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "topology": global_config["topology"], "shuffle_seed": int(runtime.get("shuffle_seed", 477)),
        "config_file": str(config_path.resolve()), "config_snapshot": config_snapshot.name,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "benchmark_commit": global_config.get("benchmark_commit"),
        "tuning_state": global_config["tuning_state"], "environment": environment,
        "qualification_order": [asdict(item) for item in qualification],
        "candidate_comparative_order": [asdict(item) for item in comparative],
        "eligible_rates_pps": None, "frozen_comparative_order": None, "runs": [],
    }
    manifest_path = session_dir / "session_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    try:
        if not skip_build:
            progress("Building configured binaries on the benchmark nodes")
            build_nodes([receiver_node, sender_node], global_config["remote_project_root"], runtime)
            progress("Remote builds complete")
        completed_runs = 0
        planned_runs = len(qualification) + len(comparative)
        runs_started = time.monotonic()

        def execute_with_progress(item: RunTuple, phase: str) -> dict[str, Any]:
            nonlocal completed_runs
            benchmark = config["benchmarks"][item.benchmark_index]
            silence_seconds = (float(benchmark["sender"]["duration_seconds"]) +
                               float(runtime.get("post_sender_drain_seconds", 1.0)))
            position = completed_runs + 1
            label = (f"{phase} {item.benchmark_name} rep={item.repetition} "
                     f"rate={item.rate}pps burst={item.burst} batch={item.batch}")
            progress(f"START {position}/{planned_runs}: {label}; timed interval will be "
                     f"quiet for {format_duration(silence_seconds)}")
            run_started = time.monotonic()
            try:
                result = execute_run(item, config, session_id, session_dir,
                                     receiver_node, sender_node, environment)
            except Exception as exc:
                progress(f"FAILED {position}/{planned_runs}: {label} after "
                         f"{format_duration(time.monotonic() - run_started)}: {exc}")
                raise
            completed_runs += 1
            metadata = result["metadata_object"]
            validity = metadata["validity"]
            accounting = metadata["packet_accounting"]
            status = "valid" if validity["valid"] else "INVALID: " + "; ".join(validity["reasons"])
            average_seconds = (time.monotonic() - runs_started) / completed_runs
            eta_seconds = average_seconds * max(0, planned_runs - completed_runs)
            progress(f"DONE {completed_runs}/{planned_runs}: {label}; {status}; "
                     f"sent={metadata['sender_stats']['successful_sends']} "
                     f"received={metadata['receiver_stats']['unique_valid_packets']} "
                     f"processed={metadata['receiver_stats']['unique_processed_packets']} "
                     f"app_loss={accounting['application_loss_pct']:.4f}%; "
                     f"run={format_duration(time.monotonic() - run_started)} "
                     f"ETA={format_duration(eta_seconds)}")
            return result

        qualification_metadata: list[dict[str, Any]] = []
        for item in qualification:
            result = execute_with_progress(item, "qualification")
            qualification_metadata.append(result.pop("metadata_object"))
            manifest["runs"].append(result)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        eligible = qualification_rates(qualification_metadata) if qualification else None
        if eligible is not None:
            comparative = [item for item in comparative
                           if not config["benchmarks"][item.benchmark_index].get("uses_qualified_rates", False)
                           or item.rate in eligible]
        planned_runs = completed_runs + len(comparative)
        if eligible is not None:
            progress(f"Qualification complete: eligible rates={eligible}; "
                     f"{len(comparative)} comparison runs frozen")
        manifest["eligible_rates_pps"] = eligible
        manifest["frozen_comparative_order"] = [asdict(item) for item in comparative]
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        for item in comparative:
            result = execute_with_progress(item, "comparison")
            result.pop("metadata_object")
            manifest["runs"].append(result)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        progress(f"Session {session_id}: all {completed_runs} runs complete; generating summaries")
    finally:
        receiver_node.close()
        if sender_node is not receiver_node:
            sender_node.close()
    return session_dir


def preflight(config: dict[str, Any]) -> dict[str, Any]:
    validate_config(config)
    global_config, nodes = config["global"], config["global"]["nodes"]
    if global_config["topology"] != "distributed_ethernet":
        raise ValueError("preflight is only meaningful for distributed_ethernet")
    expected_commit = global_config.get("benchmark_commit")
    if not expected_commit or expected_commit == "SET_AFTER_FREEZING_COMMIT":
        raise ValueError("global.benchmark_commit must be frozen before preflight")
    report: dict[str, Any] = {"schema_version": 1, "checks": {}}
    controllers: list[NodeController] = []
    try:
        for role in ("receiver", "sender"):
            node_config = nodes[role]
            node = get_node(management_host(node_config), global_config["user"])
            controllers.append(node)
            root = global_config["remote_project_root"]
            checks = {
                "commit": probe(node, ["git", "-C", root, "rev-parse", "HEAD"]),
                "clean": probe(node, ["git", "-C", root, "status", "--porcelain"]),
                "perf": probe(node, ["perf", "--version"]),
                "chrony": probe(node, ["chronyc", "tracking"]),
                "throttled": probe(node, ["sh", "-c", "vcgencmd get_throttled 2>/dev/null || false"]),
                "capabilities": probe(node, ["getcap", *[str(binary_dir(root, global_config["runtime"]) / binary)
                    for binary in sorted(RECEIVER_BINARIES)]]) if role == "receiver" else {"value": "not applicable", "error": None},
                "ctest": probe(node, ["ctest", "--test-dir", str(binary_dir(root, global_config["runtime"])), "--output-on-failure"]),
                "pytest": probe(node, [str(Path(root) / ".venv/bin/python"), "-m", "pytest", "-q"]),
            }
            if node_config.get("interface"):
                checks["link"] = probe(node, ["ethtool", node_config["interface"]])
            complete_metadata = collect_machine_metadata(
                node, root, node_config.get("interface"))
            checks["complete_metadata"] = {"value": complete_metadata, "error": None}
            failures = []
            if checks["commit"]["value"] != expected_commit:
                failures.append("wrong commit")
            if checks["clean"]["error"] or checks["clean"]["value"]:
                failures.append("dirty tree")
            for name in ("perf", "chrony", "throttled", "ctest", "pytest"):
                if checks[name]["error"]:
                    failures.append(f"{name} unavailable or failed")
            if role == "receiver":
                capability_lines = str(checks["capabilities"]["value"]).splitlines()
                if len(capability_lines) != len(RECEIVER_BINARIES) or any(
                        "cap_sys_nice" not in line for line in capability_lines):
                    failures.append("one or more receivers lack CAP_SYS_NICE")
            link_text = str(checks.get("link", {}).get("value", ""))
            if node_config.get("interface") and ("Speed: 1000Mb/s" not in link_text or "Duplex: Full" not in link_text):
                failures.append("link is not 1 Gb/s full duplex")
            chrony_text = str(checks["chrony"]["value"])
            if "Leap status     : Normal" not in chrony_text:
                failures.append("clock is not synchronized")
            if throttled({"throttled": checks["throttled"]}):
                failures.append("throttling flag is nonzero")
            incomplete = sorted(name for name, result in complete_metadata.items()
                                if isinstance(result, dict) and result.get("error"))
            if incomplete:
                failures.append("incomplete metadata: " + ", ".join(incomplete))
            governor = str(complete_metadata.get("governor", {}).get("value", ""))
            if governor and any(not line.endswith("=performance") for line in governor.splitlines()):
                failures.append("performance governor is not active on every CPU")
            socket_maxima = str(complete_metadata.get("socket_maxima", {}).get("value", ""))
            configured_buffer = max(int(global_config["runtime"].get("receive_buffer_bytes", 0)),
                                    int(global_config["runtime"].get("send_buffer_bytes", 0)))
            maxima = [int(value) for value in re.findall(r"=\s*(\d+)", socket_maxima)]
            if len(maxima) != 2 or any(value < configured_buffer for value in maxima):
                failures.append("socket maxima are below configured buffers")
            report["checks"][role] = {"probes": checks, "failures": failures}
        all_failures = [f"{role}: {reason}" for role, value in report["checks"].items()
                        for reason in value["failures"]]
        report["valid"] = not all_failures
        report["failures"] = all_failures
        if all_failures:
            raise RuntimeError("preflight failed: " + "; ".join(all_failures))
        return report
    finally:
        for node in controllers:
            node.close()


def run_analysis(session_dir: Path) -> None:
    root = Path(__file__).parent
    subprocess.run([sys.executable, str(root / "analysis/generate_summary.py"),
                    str(session_dir)], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config_local.yaml")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-analysis", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--preflight-report", default="preflight_report.json")
    arguments = parser.parse_args()
    config_path = Path(arguments.config)
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if arguments.preflight:
            report = preflight(config)
            Path(arguments.preflight_report).write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"Preflight passed: {arguments.preflight_report}")
            return 0
        session_dir = run_campaign(config, config_path, arguments.skip_build)
        if not arguments.skip_analysis:
            run_analysis(session_dir)
        print(f"Completed session: {session_dir}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
