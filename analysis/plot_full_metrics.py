"""Plot a CDF directly from one or more versioned/legacy traces."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from latency_utils import load_binary_file, remove_clock_drift


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", action="append", nargs=2, metavar=("LABEL", "PATH"), required=True)
    parser.add_argument("--topology", choices=["local_loopback", "distributed_ethernet"], required=True)
    parser.add_argument("--out", default="receive_latency_cdf.png")
    args = parser.parse_args()
    frames = []
    for label, filename in args.trace:
        clean, _ = remove_clock_drift(load_binary_file(filename))
        frames.append(pd.DataFrame({"Receive latency (µs)": clean["latency_corrected_us"], "Variant": label}))
    data = pd.concat(frames, ignore_index=True)
    axis = sns.ecdfplot(data=data, x="Receive latency (µs)", hue="Variant")
    qualifier = "Local loopback validation" if args.topology == "local_loopback" else "Ethernet; CLOCK_REALTIME synchronization-limited"
    axis.set_title(f"Receive-latency CDF — {qualifier}")
    plt.tight_layout(); plt.savefig(Path(args.out), dpi=200)


if __name__ == "__main__":
    main()
