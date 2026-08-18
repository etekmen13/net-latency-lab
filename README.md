# net-latency-lab

An evidence-first UDP receive benchmark with three real Linux implementations:
synchronous `recvfrom`, synchronous `recvmmsg`, and `recvmmsg` with an SPSC
worker. The repository is ready for its predeclared two–Raspberry Pi 4 run, but
contains no physical results or numerical performance claim yet. Loopback data
is correctness evidence only.

The benchmark keeps requested, successfully offered, uniquely received,
uniquely processed, ingress-lost, and application-lost packet rates distinct.
Throughput runs write no per-packet records; workload runs sample every hundredth
packet while all packets remain in online sequence/drop accounting. Timed
distributed intervals contain no SSH polling or control traffic.

## Build and correctness checks

Install Debian/DietPi dependencies, including system GoogleTest:

```sh
sudo apt-get update
sudo apt-get install build-essential cmake libgtest-dev python3-venv chrony ethtool iproute2 linux-perf libcap2-bin
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cmake --preset dev
cmake --build --preset dev -j2
ctest --preset dev
.venv/bin/python -m pytest -q

cmake --preset tsan
cmake --build --preset tsan -j2
ctest --preset tsan
```

GoogleTest is found from `libgtest-dev`; CMake never downloads or vendors it.
CI runs the normal native/Python suite and the SPSC stress/drain tests under
ThreadSanitizer. Raspberry Pi release builds use `pi4-release` (`Cortex-A72`,
debug symbols, frame pointers).

Native tests cover exact little-endian header/record bytes, round trips,
unsupported versions/truncation, SPSC empty/full/wraparound, two million ordered
cross-thread transfers, and shutdown draining. Python tests cover CLIs, all
receivers, sender behavior, loopback, binary compatibility, analysis, and fake
orchestration. The [binary format](docs/binary_log_format.md) retains legacy
28-byte reader support.

## Local smoke run

```sh
./run_lab.sh --config config_debug.yaml
```

This exercises all three binaries and produces an ignored session under
`results/sessions/`. It does not generate recruiter-facing figures or claims.
Individual options are available through `--help`; notable controls are
`--sample-every`, `--socket-buffer`, `--work`, `--batch`, CPU affinity, and
scheduler policy. The sender additionally supports adaptive `sendmmsg` through
`--send-batch-max` and `--batch-window-us`, buffered `--pacing-trace`, diagnostic
`--mode flood`, and optional phase-staggered `--threads`/`--cpus` workers.

## Physical Raspberry Pi workflow

The [frozen protocol and operator runbook](docs/experiment_plan.md) specifies
two actively cooled Raspberry Pi 4 systems running DietPi v9.17.2, ARM64,
based on Debian 13 (Trixie), with direct 1 GbE at
`192.168.50.1/30`–`192.168.50.2/30`, Wi-Fi management, fixed CPU roles, 4 MiB
buffers, seed 477, qualification, five-repetition raw/workload campaigns, and
separate profiling.

After setting only management addresses and the candidate commit, qualify the
generator and run the non-claim pilot in separate sessions. Preserve failed
sessions. Try batch windows 10, 25, 50, then 100 microseconds and stop at the
first configuration whose complete grid passes; use two sender workers only if
single-core batching fails, and `external_generator` with a dedicated x86 NIC
only if both Pi stages fail.

```sh
./run_lab.sh --config config_sender_qualification.yaml --preflight
./run_lab.sh --config config_sender_qualification.yaml
./run_lab.sh --config config_pilot.yaml

# Freeze the passing protocol and parameters in a new benchmark commit, update
# benchmark_commit, then collect an entirely fresh final campaign.
./run_lab.sh --config config_distributed.yaml --preflight
./run_lab.sh --config config_distributed.yaml

python analysis/profile_tools.py prepare results/sessions/MEASUREMENT_SESSION \
  config_distributed.yaml profile_config.yaml
./run_lab.sh --config profile_config.yaml
python analysis/profile_tools.py summarize results/sessions/PROFILE_SESSION

python analysis/publish_results.py results/sessions/MEASUREMENT_SESSION \
  results/sessions/PROFILE_SESSION results/pi4-YYYY-MM-DD
```

The harness freezes tuple order before launching, strictly qualifies the sender
grid (rate, accounting, counters, link/throttle state, and 1 ms pacing), waits
silently for sender duration plus a one-second drain, and
then collects status/artifacts. Each run records requested/observed socket
buffers, monotonic receive/process windows, drain/backlog, thermal/throttle and
clock state, link/duplex/offloads, IRQ affinity, system counters, process
outcomes, and a validity verdict.

Profiling selects batches mechanically, runs three `perf stat` repetitions and
one call-graph capture per architecture, and marks unsupported kernel events as
unavailable. `perf.data` and raw traces stay outside Git; `perf report --stdio`
extracts and SHA-256 checksums are publishable.

Profile lifecycle validation is available as
`scripts/profile_shutdown_smoke.sh`. The harness records the actual receiver
PID separately from the `perf` wrapper PID and signals only the receiver during
graceful profile shutdown, preserving receiver stats and finalized perf data.

## Publication boundary

Publication produces one cleaned benchmark CSV, `claim_evidence.csv`, exactly
two figures (throughput/loss and profile mechanism), environment data, profile
reports, and checksums. A resume sentence is emitted only when all five runs
pass the ≤0.1% application-loss rule and profile counters support the proposed
mechanism. Observations and explanations remain separate. Cross-host
`CLOCK_REALTIME` latency is synchronization-limited and is not used for a claim
without materially smaller recorded clock uncertainty.

See [results/README.md](results/README.md). Old sessions remain immutable
historical evidence and must not be combined with the new campaign. Until the
fresh physical runs and explicit review complete, no Raspberry Pi performance
result is published.
