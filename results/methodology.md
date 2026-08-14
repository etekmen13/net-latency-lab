# Measurement methodology

The authoritative predeclared protocol is
[`../docs/experiment_plan.md`](../docs/experiment_plan.md), and the binary layout
is [`../docs/binary_log_format.md`](../docs/binary_log_format.md). Results are
publishable only through `analysis/publish_results.py`, which rejects incomplete
physical campaigns and produces exactly two recruiter-facing figures.

The physical benchmark environment is two Raspberry Pi 4 systems running
DietPi v9.17.2, ARM64, based on Debian 13 (Trixie).

No physical Raspberry Pi measurements have been committed yet. Consequently,
this repository makes no numerical performance or resume claim.
