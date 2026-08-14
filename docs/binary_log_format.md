# Binary trace format

New traces are architecture-independent version 1 files. Every integer is
unsigned and encoded explicitly in little-endian order; C++ structure layout is
never written to disk.

| Offset | Bytes | Field |
|---:|---:|---|
| 0 | 8 | `NLLOG\0\r\n` magic |
| 8 | 2 | version (`1`) |
| 10 | 2 | header size (`16`) |
| 12 | 4 | record size (`36`) |

Each record contains `sequence` (`u32`), followed by four `u64`
`CLOCK_REALTIME` nanosecond timestamps: transmit, receive, processing start, and
processing finish. The Python reader validates the version and exact record
length. It also retains read-only compatibility with original headerless
28-byte little-endian records (`u32 sequence`, `u64 transmit`, `u64 receive`,
signed `i64` cached latency).

Throughput runs use `--sample-every 0`, so their trace contains only the header.
All packet, sequence, loss, and rate accounting comes from online receiver
counters. Workload/latency runs use `--sample-every 100`; sampling affects only
trace writes, never accounting or processing. Sampling is deterministic by
sequence number (`sequence % N == 0`). The sender uses the matching
`--timestamp-every` value, eliminating per-packet `CLOCK_REALTIME` reads from
count-only throughput and profiling runs.
