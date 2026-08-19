# Candidate resume claims — WORKING DRAFT

**This is not published evidence.** It is a running list of findings from this
project that may be worth putting in front of a quant/HFT screen, with an honest
grade on each. Nothing here is a published result: the repository's publication
boundary still applies, and any number quoted below is provisional until
`claim_evidence.csv` emits it with an empty `claim_blockers` column.

Every figure carries its provenance. If a line has no session behind it, it is
marked `[unverified]` and must not be used.

Audience assumption: HFT / market-making engineering. That audience does not care
about absolute packets-per-second on a Raspberry Pi — they run 10/25 GbE on Xeons.
They care about tail latency, loss attribution, mechanism (syscalls, cycles, cache
lines), and evidence that you distrust your own instruments.

---

## Tier 1 — candidates for the resume itself

Pick 3–4. Written as prose, ready to adapt.

**1. Instrumentation distrust.** *(strongest thing in the project)*

> Found the UDP load generator's own success counter over-reported offered load by
> 2.5× — 949,909 pps claimed against 376,686 actually on the wire — because
> `udp_sendmsg` reports a full-qdisc `ENOBUFS` as success unless `IP_RECVERR` is
> set. Rebuilt offered-load accounting to reconcile against the NIC's `tx_packets`
> counter, now exact to within 1 packet.

Why it lands: every desk has shipped a wrong number from a trusted counter. This
says you assume your measuring device is lying until it agrees with an independent
one. It is also a genuine, non-obvious kernel behaviour.

**2. Exact loss attribution.**

> Demonstrated that a lock-free SPSC receive pipeline eliminates kernel-level packet
> loss entirely — zero ingress drops across all 21 runs, up to 1.35× the receiver's
> service rate — relocating loss to an application-visible counter that reconciles
> exactly: `received − processed − queue_overflow = 0`, maximum deviation 0 packets
> over 4.6M-packet runs.

Why it lands: "I know precisely which packets I lost and where" is the risk-systems
mindset. The exact identity is what makes it a result rather than an assertion.

**3. Predeclared model, confirmed.**

> Predeclared a two-stage pipeline model of zero-loss receive capacity
> (`1/knee = r + W`) before collecting data, and confirmed it within 6% across three
> receive architectures: fitted per-packet receive cost 1.556 µs for `recvfrom` vs
> 1.080 µs for `recvmmsg`(batch 64), with a 0.287 µs SPSC handoff.

Why it lands: prediction → confirmation is science. It also shows you can reason
about a system quantitatively before touching it, which is the actual job.

**4. Removing biases that favoured my own hypothesis.**

> Identified and removed three measurement biases that all favoured the architecture
> under test: unequal receive buffers (the "optimized" variants copied 16 B per
> packet against the baseline's 64 KiB), a sequence-accounting structure inside the
> timed path whose 563 KiB working set exceeded L2 and which charged the two
> architectures unequally, and 802.3x pause frames (7.2M exchanged) that made
> offered load a dependent variable rather than an independent one.

Why it lands: finding a bias that would have made your result look *better*, and
removing it, is the single most credible thing an experimentalist can report.

**5. Tail latency.** `[PENDING — D6 campaign not yet run]`

> p99.9 application queueing delay of __ µs at matched offered load; quantified the
> throughput-versus-tail-latency exchange rate of syscall batching across batch
> sizes.

This is the most quant-shaped artifact the project can produce and is the one gap
in the current set. Fill from the `latency_5us` campaign.

---

## Tier 2 — interview ammunition, not resume lines

Bring these out when they dig. Each is a complete story with a number.

- **The optimized design initially lost.** Pipelining only pays when the handoff
  costs less than the receive work it removes (`h < r`). Measured `h ≈ 2.3 µs`
  against `r = 1.56 µs`, so the SPSC receiver was the *slowest* of the three. Fixing
  the handoff (see below) took it to 0.287 µs and reversed the result.
- **`cpu_relax()` emitted `isb` on aarch64** — an Instruction Synchronization
  Barrier, i.e. a pipeline flush, where the ARM spin-wait hint is `yield`. It sat in
  both the SPSC poll loop and the synthetic-work loop, coarsening the very work
  quantum the experiment sets.
- **Load-adaptive syscall amortization.** `recvmmsg` with `MSG_WAITFORONE` returns as
  soon as one message is available, so its benefit is *zero* below the knee and
  maximal at it: packets per syscall goes from ~5 to 63.95 exactly as the receiver
  saturates. The mechanism is self-stabilizing, and it is measured directly rather
  than inferred from cycle counts.
- **Detecting that the receivers were never saturated.** All three variants tied at
  ~386 kpps, which looked like a receiver limit and was actually the *generator's*
  ceiling. A tie across architectures is a signature of a sender-limited sweep. This
  is now a hard gate: a configuration is only "receiver-limited" if a strictly higher
  offered rate was tested and failed.
- **Real-time throttling as a hidden architectural bias.** The default
  `sched_rt_runtime_us` throttles a saturated `SCHED_FIFO` thread for 50 ms of every
  second — and penalises the *threaded* variant twice, because its two threads are
  throttled independently. It would have appeared in the data as an architecture
  difference.
- **Why the speedup is only ~1.24×, and why that is the ceiling.** Two-stage pipeline
  speedup is `(r + W)/max(r, W)`; with `r = 1.556 µs` against a 5 µs work budget the
  theoretical maximum is 1.31×, so the implementation is at 95% of its structural
  limit. Getting a bigger number requires *balancing* the stages, which requires a
  smaller work budget, which requires a faster generator than a Pi can be. Knowing
  which constraint binds is the point.
- **Multi-threaded generation is unusable, and not fixably so.** Workers allocate
  sequence blocks from one shared atomic and emit them on separate sockets, so 44–59%
  of packets arrive reordered even at rates with zero loss. Striping per worker makes
  it worse, not better; multiple sockets means multiple independent qdisc enqueues
  and the aggregate stream cannot be monotone.
- **Clock synchronisation without hardware timestamping** — see Tier 3, item 1. Good
  as an answer, weak as a bullet.
- **Harness correctness:** a paramiko deadlock (`recv_exit_status()` before draining
  stdout hangs past the 2 MiB channel window, which also made every `timeout=`
  argument in the codebase unenforceable dead code); a claim gate that passed on
  floating-point noise (`improvement > 1.0` evaluating True on 1.0000000031); and a
  mechanism gate that was *unpassable by construction* for the threaded variant,
  because process-scope `perf` counts its busy-polling worker thread so cycles per
  packet is necessarily higher.

---

## Tier 3 — honest assessment: probably not worth a bullet

**1. Chrony peer-sync instead of public NTP.** The receiver is disciplined directly
to the sender over the benchmark link rather than to internet NTP, achieving **301 ns
RMS offset** (vs the sender's own 543 µs against WAN NTP), which makes cross-host
one-way latency measurable without hardware timestamping.

*Verdict:* excellent engineering, weak resume line. Standing alone it reads as "I
configured NTP correctly," and a reviewer cannot tell the difference between that and
what you actually did. It is strong as a **clause inside the latency bullet** — "…
measured with the receiver disciplined to the sender at 301 ns RMS, with the residual
path asymmetry reported as an uncertainty budget rather than claimed away" — because
there it is doing work: it is the reason the latency number is trustworthy. Keep it
in your pocket for the inevitable "how do you know your clocks agreed?"

**2. The SSH/orchestration engineering.** Reducing 11 SSH round trips per counter
snapshot to 1, bounded transport-only retry, the no-control-traffic-during-timed-
intervals invariant. Solid work, but it reads as devops rather than as quantitative
engineering. Belongs in the repo README, not the resume.

**3. Absolute throughput numbers.** 152k / 164k / 189k pps. Never lead with these.
On a Pi they are unimpressive in isolation and invite the wrong comparison.

---

## The counterweight to state before they find it

The pipelined receiver uses **two cores for 1.24×, i.e. 0.62× per core — worse than
the baseline.** Put this on the chart yourself. Its value is not efficiency; it is
decoupling the wire from the application and making loss countable. Volunteering the
weakness is what makes the rest credible.

---

## Provenance

| Figure | Source |
|---|---|
| 949,909 claimed vs 376,686 wire pps | sender stats vs NIC `tx_packets`, pre-`IP_RECVERR` |
| r = 1.556 / 1.080 µs, handoff 0.287 µs | `session_20260818T161946_132709Z`, overload plateau of `processing_window_pps` |
| service rates 152,528 / 164,471 / 189,144 pps | same session |
| 0 ingress drops, identity exact to 0 packets | same session, 21 threaded runs |
| packets/syscall 5 → 63.95 | same session, `packets_per_receive_syscall` |
| 7,209,416 pause frames, 51,314 `rx_missed_errors` | `ethtool -S eth0`, both nodes, pre-fix |
| chrony 301 ns RMS | `session_manifest.json` → `environment.receiver.chrony_tracking` |
| 44–59% reordering under multi-threaded send | scratch probes, 2026-08-17/18 |
| 563 KiB sequence-tracker working set | 30 s × 150 kpps ÷ 4096 per block × 512 B |

## Still to come

- **D6 latency campaign** → fills Tier 1 item 5 and the Pareto figure.
- **A2 SPSC optimization** → `buffer_[0]` shares a cache line with `tail_`, and
  `try_alloc`/`front()` load the peer index on every element (one cross-core
  coherence miss per packet per side). Worth ~5.7%. **Do not claim until measured.**
