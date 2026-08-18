from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from latency_utils import load_binary_file


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0)); return sock.getsockname()[1]


def packet(sequence: int, size: int = 64) -> bytes:
    header = struct.pack("!HBBIQ", 0x6584, 1, 0, sequence, time.time_ns())
    return header + bytes(size - len(header))


def wait_for_udp_bind(process: subprocess.Popen, port: int, timeout: float = 3.0):
    expected = f":{port:04X}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"receiver exited before binding: {stdout}{stderr}")
        entries = Path("/proc/net/udp").read_text().splitlines()[1:]
        if any(line.split()[1].endswith(expected) for line in entries):
            return
        time.sleep(0.005)
    raise TimeoutError(f"receiver did not bind UDP port {port}")


def run_receiver(binary, tmp_path, count: int, work: int = 0, batch: int = 8,
                 shutdown_with_signal: bool = False):
    port = free_port(); trace = tmp_path / f"{binary.name}_{work}.bin"; stats = tmp_path / f"{binary.name}_{work}.json"
    command = [binary, "--port", str(port), "--output", trace, "--stats", stats,
               "--max-packets", "0" if shutdown_with_signal else str(count),
               "--work", str(work)]
    if binary.name != "receiver_baseline": command += ["--batch", str(batch)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_for_udp_bind(process, port)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for sequence in range(count): sender.sendto(packet(sequence), ("127.0.0.1", port))
        if shutdown_with_signal:
            # UDP may drop part of a deliberate burst, especially on a Pi.
            # This test needs a substantial queue, not exact datagram delivery.
            time.sleep(0.5)
            process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=8)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, stdout + stderr
    return load_binary_file(trace), json.loads(stats.read_text())


@pytest.mark.parametrize("name", ["receiver_baseline", "receiver_batched", "receiver_threaded"])
def test_known_count_timestamp_order_and_stats(binaries, tmp_path, name):
    frame, stats = run_receiver(binaries[name], tmp_path, 64)
    assert stats["datagrams_received"] == 64
    assert stats["valid_packets"] == stats["processed_packets"] == len(frame) == 64
    assert stats["unique_valid_packets"] == stats["unique_processed_packets"] == 64
    assert stats["receive_sequence_gaps"] == stats["receive_duplicates"] == 0
    assert stats["short_packets"] == stats["invalid_magic"] == stats["unsupported_version"] == stats["spsc_overflow"] == 0
    assert (frame.rx_ns >= frame.tx_ns).all()
    assert (frame.processing_start_ns >= frame.rx_ns).all()
    assert (frame.processing_finish_ns >= frame.processing_start_ns).all()
    identity = frame.receive_latency_ns + frame.application_queue_delay_ns + frame.processing_time_ns
    assert (identity == frame.total_application_latency_ns).all()


@pytest.mark.parametrize("name", ["receiver_baseline", "receiver_batched", "receiver_threaded"])
def test_work_increases_recorded_processing_time(binaries, tmp_path, name):
    zero, _ = run_receiver(binaries[name], tmp_path, 40, work=0)
    worked, _ = run_receiver(binaries[name], tmp_path, 40, work=200_000)
    assert worked.processing_time_ns.median() >= 180_000
    assert worked.processing_time_ns.median() > zero.processing_time_ns.median() + 100_000


def test_threaded_receive_timestamp_survives_queue_backlog(binaries, tmp_path):
    frame, stats = run_receiver(binaries["receiver_threaded"], tmp_path, 500,
                                work=200_000, batch=64, shutdown_with_signal=True)
    # A worker-side timestamp would keep this interval near zero. The ingress
    # timestamp must expose the queue built by the deliberately slow worker.
    assert stats["interrupted"] is True
    assert stats["processed_packets"] == len(frame) >= 256
    assert stats["spsc_overflow"] == 0
    assert frame.application_queue_delay_ns.quantile(0.90) > 1_000_000


def test_threaded_sigint_drains_and_flushes(binaries, tmp_path):
    port = free_port(); trace = tmp_path / "signal.bin"; stats_path = tmp_path / "signal.json"
    process = subprocess.Popen([binaries["receiver_threaded"], "--port", str(port),
        "--output", trace, "--stats", stats_path, "--batch", "32", "--work", "50000"])
    wait_for_udp_bind(process, port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        for sequence in range(500): sender.sendto(packet(sequence), ("127.0.0.1", port))
    time.sleep(0.05); process.send_signal(signal.SIGINT); process.wait(timeout=5)
    stats = json.loads(stats_path.read_text()); frame = load_binary_file(trace)
    assert process.returncode == 0 and stats["interrupted"] is True
    assert stats["processed_packets"] == len(frame)
    assert stats["processed_packets"] + stats["spsc_overflow"] == stats["valid_packets"]


def test_threaded_fifo_affinity_lifecycle(binaries, tmp_path):
    allowed_cpus = sorted(os.sched_getaffinity(0))
    if len(allowed_cpus) < 2:
        pytest.skip("threaded FIFO lifecycle needs two allowed CPUs")

    configured_binary = os.environ.get("NLL_THREADED_RECEIVER_BINARY")
    binary = (Path(configured_binary).resolve() if configured_binary
              else binaries["receiver_threaded"])
    if not binary.is_file():
        pytest.fail(f"threaded receiver binary does not exist: {binary}")

    receiver_cpu, worker_cpu = allowed_cpus[:2]
    port = free_port()
    trace = tmp_path / "fifo_lifecycle.bin"
    stats_path = tmp_path / "fifo_lifecycle.json"
    process = subprocess.Popen([
        binary, "--port", str(port), "--output", trace, "--stats", stats_path,
        "--cpu", str(receiver_cpu), "--worker-cpu", str(worker_cpu),
        "--scheduler", "fifo", "--priority", "90", "--batch", "8",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started_shutdown = None
    try:
        wait_for_udp_bind(process, port)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for sequence in range(64):
                sender.sendto(packet(sequence), ("127.0.0.1", port))
                time.sleep(0.0001)
        time.sleep(0.05)
        started_shutdown = time.monotonic()
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pytest.fail("FIFO receiver did not exit within 5 seconds of SIGINT")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, stdout + stderr
    assert started_shutdown is not None and time.monotonic() - started_shutdown < 5
    assert stats_path.is_file(), stdout + stderr
    stats = json.loads(stats_path.read_text())

    scheduler_outcomes = [stats["receiver_scheduler"], stats["worker_scheduler"]]
    if all(not outcome["success"] and outcome["error"] in {
            "Operation not permitted", "Permission denied"}
           for outcome in scheduler_outcomes):
        pytest.skip("CAP_SYS_NICE is unavailable to the threaded receiver")

    assert stats["interrupted"] is True
    assert stats["datagrams_received"] == stats["valid_packets"]
    assert stats["processed_packets"] == stats["valid_packets"] >= 1
    assert len(load_binary_file(trace)) == stats["processed_packets"]
    for name, cpu in (("receiver_affinity", receiver_cpu),
                      ("worker_affinity", worker_cpu)):
        outcome = stats[name]
        assert outcome["success"] is True
        assert outcome["requested_cpu"] == outcome["observed_cpu"] == cpu
        assert outcome["observed_cpu_set"] == str(cpu)
    for outcome in scheduler_outcomes:
        assert outcome["success"] is True
        assert outcome["requested_policy"] == outcome["observed_policy"] == "fifo"
        assert outcome["requested_priority"] == outcome["observed_priority"] == 90


def test_count_only_mode_writes_header_and_keeps_online_accounting(binaries, tmp_path):
    port = free_port(); trace = tmp_path / "counts.bin"; stats_path = tmp_path / "counts.json"
    process = subprocess.Popen([binaries["receiver_batched"], "--port", str(port),
        "--output", trace, "--stats", stats_path, "--batch", "8",
        "--sample-every", "0", "--max-packets", "50"])
    wait_for_udp_bind(process, port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        for sequence in range(50):
            sender.sendto(packet(sequence), ("127.0.0.1", port))
            time.sleep(0.0001)
    process.wait(timeout=5)
    stats = json.loads(stats_path.read_text())
    assert process.returncode == 0 and len(load_binary_file(trace)) == 0
    assert stats["unique_valid_packets"] == stats["unique_processed_packets"] == 50
    assert stats["sampled_packets"] == 0


def test_spsc_pressure_is_reported_separately(binaries, tmp_path):
    port = free_port(); trace = tmp_path / "pressure.bin"; stats_path = tmp_path / "pressure.json"
    process = subprocess.Popen([binaries["receiver_threaded"], "--port", str(port),
        "--output", trace, "--stats", stats_path, "--batch", "64", "--work", "300000"])
    wait_for_udp_bind(process, port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        for sequence in range(20_000): sender.sendto(packet(sequence), ("127.0.0.1", port))
    time.sleep(0.1); process.send_signal(signal.SIGINT); process.wait(timeout=8)
    stats = json.loads(stats_path.read_text())
    assert stats["spsc_overflow"] > 0
    assert stats["processed_packets"] + stats["spsc_overflow"] == stats["valid_packets"]


def test_sender_stats_payload_sequence_steady_and_burst(binaries, tmp_path):
    for mode, burst in (("steady", 1), ("burst", 5)):
        port = free_port(); received = []; stop = threading.Event()
        def receive():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
                server.bind(("127.0.0.1", port)); server.settimeout(0.02)
                while not stop.is_set():
                    try: received.append(server.recvfrom(65535)[0])
                    except TimeoutError: pass
        thread = threading.Thread(target=receive); thread.start()
        stats_path = tmp_path / f"sender_{mode}.json"
        result = subprocess.run([binaries["sender"], "--ip", "127.0.0.1", "--port", str(port),
            "--rate", "1000", "--duration", "0.08", "--mode", mode, "--burst", str(burst),
            "--payload-size", "128", "--stats", stats_path], capture_output=True, timeout=3)
        time.sleep(0.05); stop.set(); thread.join()
        assert result.returncode == 0
        stats = json.loads(stats_path.read_text())
        assert stats["attempted_sends"] == stats["successful_sends"] + stats["failed_sends"]
        assert stats["successful_bytes"] == stats["successful_sends"] * 128
        assert stats["elapsed_seconds"] > 0 and stats["achieved_successful_send_pps"] > 0
        assert received and all(len(value) == 128 for value in received)
        sequences = [struct.unpack("!HBBIQ", value[:16])[3] for value in received]
        assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))
        if mode == "burst": assert stats["attempted_sends"] % burst == 0


def test_sender_can_disable_timestamp_clock_reads(binaries, tmp_path):
    port = free_port(); received = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", port)); server.settimeout(1)
        stats_path = tmp_path / "sender_no_timestamps.json"
        process = subprocess.Popen([binaries["sender"], "--ip", "127.0.0.1",
            "--port", str(port), "--rate", "100", "--duration", ".03",
            "--timestamp-every", "0", "--stats", stats_path])
        while process.poll() is None:
            try: received.append(server.recvfrom(65535)[0])
            except TimeoutError: pass
        process.wait()
    assert process.returncode == 0 and received
    assert all(struct.unpack("!HBBIQ", value[:16])[4] == 0 for value in received)


def test_sender_failures_are_counted(binaries, tmp_path):
    stats_path = tmp_path / "failed.json"
    result = subprocess.run([binaries["sender"], "--ip", "255.255.255.255", "--rate", "100",
        "--duration", "0.03", "--stats", stats_path], timeout=3)
    assert result.returncode == 0
    stats = json.loads(stats_path.read_text())
    assert stats["attempted_sends"] > 0 and stats["successful_sends"] == 0
    assert stats["failed_sends"] == stats["attempted_sends"]


@pytest.mark.parametrize("batch", [1, 4, 16, 64])
def test_sender_sendmmsg_batches_preserve_payload_sequence_and_sampling(
        binaries, tmp_path, batch):
    port = free_port(); received = []; stop = threading.Event()
    def receive():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            # This assertion counts every packet, so the socket must absorb the
            # whole burst even when a loaded runner descheduled this thread.
            server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
            server.bind(("127.0.0.1", port)); server.settimeout(0.02)
            while not stop.is_set():
                try: received.append(server.recvfrom(65535)[0])
                except TimeoutError: pass
    thread = threading.Thread(target=receive); thread.start()
    stats_path = tmp_path / f"batch-{batch}.json"
    trace_path = tmp_path / f"batch-{batch}.csv"
    result = subprocess.run([
        binaries["sender"], "--ip", "127.0.0.1", "--port", str(port),
        "--rate", "20000", "--duration", ".1", "--payload-size", "128",
        "--timestamp-every", "7", "--send-batch-max", str(batch),
        "--batch-window-us", "50", "--pacing-trace", trace_path,
        "--stats", stats_path], capture_output=True, timeout=3)
    time.sleep(.05); stop.set(); thread.join()
    assert result.returncode == 0
    stats = json.loads(stats_path.read_text())
    assert stats["successful_sends"] == stats["attempted_sends"] == 2000
    assert stats["failed_sends"] == stats["error_returns"] == 0
    assert stats["syscall_count"] <= stats["successful_sends"]
    assert sum(int(size) * count for size, count in
               stats["effective_batch_size_histogram"].items()) == 2000
    assert stats["pacing_trace"]["records"] == stats["syscall_count"]
    assert len(received) == 2000 and all(len(payload) == 128 for payload in received)
    sequences = [struct.unpack("!HBBIQ", payload[:16])[3] for payload in received]
    assert len(sequences) == len(set(sequences))
    assert sorted(sequences) == list(range(2000))
    timestamps = [struct.unpack("!HBBIQ", payload[:16])[4] for payload in received]
    assert all((timestamp != 0) == (sequence % 7 == 0)
               for sequence, timestamp in zip(sequences, timestamps))


def test_sender_flood_mode_and_two_worker_sequences(binaries, tmp_path):
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 2:
        pytest.skip("two-worker sender needs two allowed CPUs")
    port = free_port(); received = []; stop = threading.Event()
    def receive():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", port)); server.settimeout(.02)
            while not stop.is_set():
                try: received.append(server.recvfrom(65535)[0])
                except TimeoutError: pass
    thread = threading.Thread(target=receive); thread.start()
    stats_path = tmp_path / "flood.json"
    result = subprocess.run([
        binaries["sender"], "--ip", "127.0.0.1", "--port", str(port),
        "--mode", "flood", "--rate", "1000", "--duration", ".03",
        "--send-batch-max", "16", "--threads", "2", "--cpus",
        f"{allowed[0]},{allowed[1]}", "--stats", stats_path],
        capture_output=True, timeout=3)
    time.sleep(.05); stop.set(); thread.join()
    assert result.returncode == 0
    stats = json.loads(stats_path.read_text())
    assert stats["successful_sends"] > 1000
    assert stats["attempted_sends"] == (stats["successful_sends"] +
                                         stats["failed_sends"])
    assert (stats["error_returns"] > 0) == (stats["failed_sends"] > 0)
    assert len(stats["thread_outcomes"]) == 2
    sequences = [struct.unpack("!HBBIQ", payload[:16])[3] for payload in received]
    assert len(sequences) == len(set(sequences))
    # Flood mode allocates a sequence per attempt from one shared counter, so a
    # dropped attempt still consumes its number.
    assert sequences and all(sequence < stats["attempted_sends"] for sequence in sequences)
