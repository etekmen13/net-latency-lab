# Results publication boundary

No Raspberry Pi benchmark results are committed yet. Local sessions under the
ignored `results/sessions/` tree must never be promoted as performance data.

After measurement and profiling, `analysis/publish_results.py` validates five
repetitions for every comparative tuple and creates a named publication folder
containing:

- `benchmark_summary.csv` with every rate, implementation, and batch;
- `claim_evidence.csv` with exact run IDs, formulas, profile mechanism, and any
  mechanically eligible resume wording;
- `figure1_throughput_loss.png` and `figure2_profile_mechanism.png`—exactly two
  recruiter-facing figures;
- environment/benchmark-commit metadata and `perf report --stdio` extracts;
- a SHA-256 manifest covering externally archived raw sessions and `perf.data`.

Raw `.bin` and `.perf.data` files are ignored. The later results-publication
commit must record the earlier clean benchmark commit explicitly.
