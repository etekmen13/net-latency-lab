from __future__ import annotations

import subprocess

import pytest


@pytest.mark.parametrize("name", ["receiver_baseline", "receiver_batched", "receiver_threaded"])
def test_receiver_help_and_work_aliases(binaries, name):
    binary = binaries[name]
    help_result = subprocess.run([binary, "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0
    assert "--work" in help_result.stdout and "-W" in help_result.stdout
    assert subprocess.run([binary, "--work", "1", "--help"], capture_output=True).returncode == 0
    assert subprocess.run([binary, "-W", "1", "--help"], capture_output=True).returncode == 0


@pytest.mark.parametrize("name", ["receiver_baseline", "receiver_batched", "receiver_threaded"])
@pytest.mark.parametrize("arguments", [["--work"], ["--work", "bad"], ["--work", "-1"]])
def test_receiver_rejects_missing_or_invalid_work(binaries, name, arguments):
    assert subprocess.run([binaries[name], *arguments], capture_output=True).returncode != 0


def test_variant_help_only_exposes_relevant_options(binaries):
    baseline = subprocess.run([binaries["receiver_baseline"], "--help"], capture_output=True, text=True).stdout
    batched = subprocess.run([binaries["receiver_batched"], "--help"], capture_output=True, text=True).stdout
    threaded = subprocess.run([binaries["receiver_threaded"], "--help"], capture_output=True, text=True).stdout
    assert "--batch" not in baseline and "--worker-cpu" not in baseline
    assert "--batch" in batched and "--worker-cpu" not in batched
    assert "--batch" in threaded and "--worker-cpu" in threaded
    assert "single-thread" not in threaded


@pytest.mark.parametrize("arguments", [["--ip", "bad"], ["--rate", "0"], ["--duration", "0"],
                                         ["--burst", "0"], ["--payload-size", "15"],
                                         ["--payload-size", "65508"],
                                         ["--send-batch-max", "0"],
                                         ["--send-batch-max", "1025"],
                                         ["--batch-window-us", "0"],
                                         ["--threads", "0"],
                                         ["--threads", "2"],
                                         ["--threads", "2", "--cpus", "0"],
                                         ["--cpus", "0,0"]])
def test_sender_validation(binaries, arguments):
    assert subprocess.run([binaries["sender"], *arguments], capture_output=True).returncode != 0


def test_sender_help_exposes_batch_flood_trace_and_thread_controls(binaries):
    result = subprocess.run([binaries["sender"], "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    for option in ("flood", "--send-batch-max", "--batch-window-us", "--threads",
                   "--cpus", "--pacing-trace"):
        assert option in result.stdout
