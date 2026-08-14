# Predeclared Raspberry Pi benchmark protocol

This protocol is frozen before physical measurement. Loopback runs are software
validation only and cannot support performance claims. The concrete grids,
durations, CPUs, buffers, and seed are machine-readable in
[`../config_distributed.yaml`](../config_distributed.yaml).

## Hardware, topology, and fixed environment

Use two actively cooled Raspberry Pi 4 systems running DietPi v9.17.2, ARM64,
based on Debian 13 (Trixie), with stable power supplies. Connect `eth0`
directly with Cat5e/Cat6 and configure sender `192.168.50.1/30`, receiver
`192.168.50.2/30`, no gateway/default route, MTU 1500, and verified 1 Gb/s full
duplex. Wi-Fi is management-only. No polling, file transfers, package activity,
or interactive SSH occurs during timed intervals.

The sender uses ordinary NTP and serves chrony to the receiver over Ethernet.
Cross-host latency remains software-synchronization-limited and is not eligible
for a resume claim unless recorded uncertainty is materially smaller than the
effect. Both systems use the same clean benchmark commit, performance governor,
4 MiB requested socket buffers and matching `rmem_max`/`wmem_max`. DietPi's
offload defaults are retained and recorded. CPU 0 handles Ethernet/Wi-Fi
IRQs, CPU 1 housekeeping, CPU 2 the worker, and CPU 3 receive/sender loops.

## Campaigns

The harness writes the randomized tuple order before launch with seed 477.

1. Sender qualification uses 64-byte payloads, zero work, threaded batch 64,
   rates 100k through 900k in 100k steps plus 950k PPS, three 10-second runs.
   A rate is eligible only if all repetitions have zero send failures and at
   least 99% achieved/requested PPS. The eligible grid is frozen in the
   manifest before comparisons.
2. Raw throughput uses zero work, count-only logging, 30 seconds, five
   repetitions, and baseline plus batched/threaded batches 1, 4, 8, 16, 32,
   and 64 at every eligible rate.
3. The ingress-isolation campaign uses 10,000 ns work and samples every 100th
   packet. It uses the same variants at 10k, 25k, 50k, 75k, 100k, 125k, 150k,
   and 200k PPS for five 30-second repetitions.
4. Profiling is separate and never contributes to throughput or latency. The
   representative batch is the highest median processed PPS among sustainable
   configurations; candidates within 2% choose the smaller batch. Load is 80%
   of the lowest selected sustainable rate. Each implementation receives three
   30-second `perf stat` runs and one frame-pointer call-graph run.

Every run has a fixed one-second post-sender drain. A run is invalid for process
failure, flush timeout, unreadable artifacts, missing required metadata,
continued application backlog, throttling, negative/unexplained counters, or
unreconciled sender/UDP/NIC/sequence/SPSC accounting.

## Sustainable rule and metrics

A configuration sustains a requested rate only when all five repetitions have
zero sender failures, offered PPS at least 99% of requested, application loss
at most 0.1%, a valid environment/process verdict, and fully reconciled
accounting. Report median processed PPS and IQR at the highest passing requested
rate. Improvement is the ratio of selected implementation and baseline medians
at their respective highest passing rates.

Offered PPS is successful full-length `sendto` calls divided by actual sender
runtime. Received and processed PPS use unique valid receives and unique
processed sequences over that same runtime. Ingress loss is successful sends
minus unique valid receives; application loss is successful sends minus unique
processed packets. Short/invalid packets, gaps, duplicates, reordering, SPSC
overflow, UDP/NIC drops, and missing counters remain separate.

`perf stat` records task clock, cycles, instructions, branches/misses, cache
references/misses, context switches, migrations, faults, and receive syscall
tracepoints when exposed by the kernel. Derived metrics are IPC, cycles,
instructions, cache misses and receive syscalls per processed packet, plus
context switches per second. Unsupported events stay unavailable. Severe event
multiplexing requires a rerun.

## Operator runbook

1. Install `git build-essential cmake libgtest-dev python3-venv chrony ethtool
   iproute2 linux-perf libcap2-bin`, configure the direct NetworkManager `/30` profile with
   `ipv4.never-default yes` and IPv6 disabled, then save `ping -I eth0`,
   `ethtool`, `chronyc tracking`, and `chronyc sources -v` output.
2. Check out the same frozen commit on both Pis, create `.venv`, install
   requirements, and run `cmake --preset pi4-release`, `cmake --build --preset
   pi4-release`, `ctest --preset pi4-release`, and `.venv/bin/python -m pytest
   -q`. Build/run the `tsan` preset before performance work.
3. From the repository root, run `sudo scripts/setup_env.sh receiver eth0` on
   the receiver and the same command with `sender` on the sender. The script
   snapshots/restores governor, IRQ, socket/perf security, Wi-Fi power, and
   receiver capability state, and applies `CAP_SYS_NICE` to final receiver
   binaries. Edit only management Wi-Fi addresses and the frozen
   commit in the distributed config.
4. Run `./run_lab.sh --config config_distributed.yaml --preflight`. It aborts
   for dirty/wrong commits, failed tests, missing perf/capability, unsynchronized
   clocks, throttle flags, or a link other than 1 Gb/s full duplex.
5. Run the measurement config unattended. Inspect qualification validity only;
   the harness freezes eligible rates. Do not change tuning or use the network.
6. Prepare profiling with `python analysis/profile_tools.py prepare SESSION
   config_distributed.yaml profile_config.yaml`, run that generated config, then
   create `profile_summary.csv` with `profile_tools.py summarize`.
7. Restore both machines with `sudo scripts/restore_env.sh`. Validate archived
   sessions and publish with `analysis/publish_results.py`. Raw traces and
   `perf.data` stay outside Git but receive SHA-256 entries; only summary data,
   two figures, environment, profile reports, claim evidence, and checksums are
   committed.

The result narrative must label observations separately from mechanisms. A
claim is emitted only when `claim_evidence.csv` passes the sustainable rule and
profile data supports fewer syscalls/cycles per packet (or another explicitly
encoded mechanism). Stop adding features after publication.
