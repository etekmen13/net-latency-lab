#!/bin/bash

# Usage: ./run_experiment.sh <experiment_name>


if [-z "$1" ]; then
  echo "Usage: $0 <experiment_name?"
  exit
fi


EXP_NAME=$1
TIMESTAMP=$(date + "%Y-%m-%d-%H-%M-%S")
GIT_HASH=$(git rev-parse --short HEAD)


BASE_DIR="../analysis/data/"
EXP_DIR="$BASE_DIR/$EXP_NAME/$TIMESTAMP"
mkdir -p "$EXP_DIR"


BIN_FILE="$EXP_DIR/data.bin"
CSV_FILE="$EXP_DIR/data.csv"
SUMMARY_FILE="$EXP_DIR/summary.csv"
PLOT_FILE="$EXP_DIR/plot.png"
METADATA_FILE="$EXP_DIR/metadata.txt"


echo "========================================"
echo "Starting Experiment: $EXP_NAME"
echo "Output Directory: $EXP_DIR"
echo "========================================"

# 2. Save Metadata (Reproducibility)
echo "Experiment: $EXP_NAME" > "$METADATA_FILE"
echo "Timestamp: $TIMESTAMP" >> "$METADATA_FILE"
echo "Git Commit: $GIT_HASH" >> "$METADATA_FILE"
echo "--------------------------------" >> "$METADATA_FILE"
echo "Compiler Config:" >> "$METADATA_FILE"
# Optional: dump cmake config or define flags here if possible

echo "[1/4] Running Receiver... (Press Ctrl+C to stop)"
../build/receiver "$BIN_FILE"

echo ""
echo "[2/4] Converting Binary to CSV..."
if [ -f "$BIN_FILE" ]; then
    python3 ../analysis/bin_to_csv.py "$BIN_FILE" "$CSV_FILE"
else
    echo "Error: No binary file generated."
    exit 1
fi

echo "[3/4] Generating Plots and Summary..."
if [ -f "$CSV_FILE" ]; then
    python3 analysis/receiver_delay.py "$CSV_FILE" --output "$EXP_DIR"
else
    echo "Error: CSV conversion failed."
    exit 1
fi

echo "[4/4] Done."
echo "Results saved to: $EXP_DIR"
