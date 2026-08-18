"""Binary-log loading and measurement helpers.

Version 1 logs have a 16-byte header followed by 36-byte records.  The loader
also accepts the original headerless 28-byte records.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np
import pandas as pd

LOG_MAGIC = b"NLLOG\x00\r\n"
LOG_VERSION = 1
HEADER_FORMAT = "<8sHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
VERSIONED_FORMAT = "<IQQQQ"
VERSIONED_SIZE = struct.calcsize(VERSIONED_FORMAT)
LEGACY_FORMAT = "<IQQq"
LEGACY_SIZE = struct.calcsize(LEGACY_FORMAT)


def _empty_frame() -> pd.DataFrame:
    columns = [
        "seq", "tx_ns", "rx_ns", "processing_start_ns",
        "processing_finish_ns", "legacy_recorded_latency_ns",
        "log_format_version",
    ]
    return _derive_metrics(pd.DataFrame(columns=columns))


def _derive_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Derive all metric names from timestamps, never from cached latency."""
    df = df.copy()
    for column in ("tx_ns", "rx_ns", "processing_start_ns", "processing_finish_ns"):
        df[column] = pd.to_numeric(df[column], errors="raise")
    df["receive_latency_ns"] = df["rx_ns"] - df["tx_ns"]
    df["application_queue_delay_ns"] = df["processing_start_ns"] - df["rx_ns"]
    df["processing_time_ns"] = df["processing_finish_ns"] - df["processing_start_ns"]
    df["total_application_latency_ns"] = df["processing_finish_ns"] - df["tx_ns"]
    for source, target in (
        ("receive_latency_ns", "receive_latency_us"),
        ("application_queue_delay_ns", "application_queue_delay_us"),
        ("processing_time_ns", "processing_time_us"),
        ("total_application_latency_ns", "total_application_latency_us"),
    ):
        df[target] = df[source] / 1000.0
    # Compatibility names used by older plotting notebooks.
    df["raw_latency_ns"] = df["receive_latency_ns"]
    df["latency_us"] = df["receive_latency_us"]
    df["rx_delta_us"] = df["rx_ns"].diff() / 1000.0
    return df


def load_binary_file(filepath: os.PathLike[str] | str) -> pd.DataFrame:
    """Load a versioned or legacy receiver log.

    Malformed/truncated input raises ``ValueError`` instead of quietly dropping
    the final record, which makes artifact corruption visible to automation.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    raw = path.read_bytes()
    if not raw:
        return _empty_frame()

    records: list[tuple[int, int, int, int, int, object, int]] = []
    if raw.startswith(LOG_MAGIC):
        if len(raw) < HEADER_SIZE:
            raise ValueError(f"Truncated binary log header: {path}")
        magic, version, header_size, entry_size = struct.unpack_from(HEADER_FORMAT, raw)
        if magic != LOG_MAGIC:
            raise ValueError(f"Invalid binary log magic: {path}")
        if version != LOG_VERSION:
            raise ValueError(f"Unsupported binary log version {version}: {path}")
        if header_size < HEADER_SIZE or header_size > len(raw):
            raise ValueError(f"Invalid binary log header size {header_size}: {path}")
        if entry_size != VERSIONED_SIZE:
            raise ValueError(f"Unsupported binary log entry size {entry_size}: {path}")
        payload = raw[header_size:]
        if len(payload) % entry_size:
            raise ValueError(f"Truncated versioned binary log record: {path}")
        for seq, tx, rx, start, finish in struct.iter_unpack(VERSIONED_FORMAT, payload):
            records.append((seq, tx, rx, start, finish, pd.NA, version))
    else:
        if len(raw) % LEGACY_SIZE:
            raise ValueError(f"Unrecognized or truncated legacy binary log: {path}")
        for seq, tx, rx, recorded_latency in struct.iter_unpack(LEGACY_FORMAT, raw):
            records.append((seq, tx, rx, rx, rx, recorded_latency, 0))

    frame = pd.DataFrame(records, columns=[
        "seq", "tx_ns", "rx_ns", "processing_start_ns",
        "processing_finish_ns", "legacy_recorded_latency_ns",
        "log_format_version",
    ])
    return _derive_metrics(frame)


def sequence_statistics(sequence: pd.Series) -> dict[str, int]:
    """Count internal gaps, duplicate records, and arrival-order reordering."""
    values = [int(value) for value in sequence]
    if not values:
        return {"sequence_gap_loss": 0, "duplicates": 0, "reordered": 0}
    duplicates = len(values) - len(set(values))
    unique = sorted(set(values))
    gaps = sum(max(0, right - left - 1) for left, right in zip(unique, unique[1:]))
    high_watermark = values[0]
    reordered = 0
    seen: set[int] = set()
    for value in values:
        if value not in seen and value < high_watermark:
            reordered += 1
        high_watermark = max(high_watermark, value)
        seen.add(value)
    return {"sequence_gap_loss": gaps, "duplicates": duplicates, "reordered": reordered}


def _drift_result(result: pd.DataFrame, corrected: pd.Series, slope: float,
                  applied: bool) -> tuple[pd.DataFrame, float]:
    """Single exit point so every drift path returns the same columns/shape."""
    result["latency_corrected_us"] = corrected
    result["jitter_us"] = result["latency_corrected_us"].diff().abs()
    result["clock_correction_applied"] = applied
    return result, float(slope)


def remove_clock_drift(df: pd.DataFrame, window_size: int = 1000) -> tuple[pd.DataFrame, float]:
    """Estimate linear sender/receiver clock skew and return ``(frame, slope)``.

    The slope is microseconds of apparent latency change per second.  Runs with
    fewer than ``window_size`` records are deliberately left uncorrected and
    marked with ``clock_correction_applied=False``.  Every return path goes
    through :func:`_drift_result`, so the tuple shape and the added columns
    (``latency_corrected_us``, ``jitter_us``, ``clock_correction_applied``) are
    identical regardless of which branch is taken.
    """
    if "receive_latency_us" not in df.columns:
        raise ValueError("Dataframe is missing receive_latency_us")
    if "rx_ns" not in df.columns:
        raise ValueError("Dataframe is missing rx_ns")
    result = df.copy()
    raw = pd.to_numeric(result["receive_latency_us"], errors="raise")
    if raw.isna().any():
        raise ValueError("receive_latency_us contains missing values")
    if len(result) < window_size:
        return _drift_result(result, raw, 0.0, False)

    rx_ns = pd.to_numeric(result["rx_ns"], errors="raise").astype("float64")
    elapsed = (rx_ns - rx_ns.iloc[0]) / 1e9
    if not np.isfinite(float(elapsed.max())) or float(elapsed.max()) <= 0.0:
        return _drift_result(result, raw, 0.0, False)

    sample_count = min(20, len(result))
    x_minima: list[float] = []
    y_minima: list[float] = []
    for indices in np.array_split(np.arange(len(result)), sample_count):
        values = raw.iloc[indices]
        index = int(indices[int(np.argmin(values.to_numpy()))])
        x_minima.append(float(elapsed.iloc[index]))
        y_minima.append(float(raw.iloc[index]))
    if len(set(x_minima)) < 2:
        return _drift_result(result, raw, 0.0, False)
    slope, _intercept = np.polyfit(x_minima, y_minima, 1)
    slope = float(slope)
    if not np.isfinite(slope):
        return _drift_result(result, raw, 0.0, False)
    return _drift_result(result, raw - slope * elapsed, slope, True)


def require_corrected_latency(df: pd.DataFrame) -> None:
    if "latency_corrected_us" not in df.columns:
        raise ValueError("Clock-drift processing did not produce latency_corrected_us")
    if df["latency_corrected_us"].isna().any():
        raise ValueError("latency_corrected_us contains missing values")
