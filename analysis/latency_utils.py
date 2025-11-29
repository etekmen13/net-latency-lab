import struct
import pandas as pd
import numpy as np
import os

# Struct format:
#   seq_idx (uint32) - 4 bytes
#   tx_ts   (uint64) - 8 bytes
#   rx_ts   (uint64) - 8 bytes
#   latency (int64)  - 8 bytes
#   Total: 28 bytes
STRUCT_FMT = "=IQQq"
STRUCT_LEN = struct.calcsize(STRUCT_FMT)


def load_binary_file(filepath):
    """
    Reads the C++ binary log and returns a Pandas DataFrame.
    """
    data = []
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(STRUCT_LEN):
                if len(chunk) == STRUCT_LEN:
                    data.append(struct.unpack(STRUCT_FMT, chunk))
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=["seq", "tx_ns", "rx_ns", "raw_latency_ns"])

    df["latency_us"] = df["raw_latency_ns"] / 1000.0

    df["rx_delta_us"] = df["rx_ns"].diff() / 1000.0

    return df


def remove_clock_drift(df, window_size=1000):
    """
    Applies Linear Skew Correction.
    Fits a line to the sliding window minimums (convex hull) to remove
    constant clock drift between the two machines.
    """
    if len(df) < window_size:
        return df

    t = (df["rx_ns"] - df["rx_ns"].iloc[0]) / 1e9
    y = df["latency_us"]

    bins = pd.cut(t, bins=20)
    min_points = df.groupby(bins, observed=False)["latency_us"].min().dropna()

    x_mins = [i.mid for i in min_points.index]
    y_mins = min_points.values

    slope, intercept = np.polyfit(x_mins, y_mins, 1)

    correction = (slope * t) + intercept

    df["latency_corrected_us"] = y - correction

    df["jitter_us"] = df["latency_corrected_us"].diff().abs()

    return df, slope
