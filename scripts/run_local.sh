#!/bin/bash

set -e 

if [[ -z "$1" ]]; then
  echo "Usage: $0 <mode>"
  echo "Modes: baseline | threaded"
  exit 1
fi

MODE=$1
RX_IP="127.0.0.1" 

if [[ "$MODE" == "baseline" ]]; then
    RECEIVER_BIN_NAME="receiver_baseline"
elif [[ "$MODE" == "threaded" ]]; then
    RECEIVER_BIN_NAME="receiver_threaded"
else
    echo "Error: Mode must be 'baseline' or 'threaded'"
    exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TIMESTAMP=$(date +"%Y-%m-%d-%H-%M-%S")
EXP_NAME="local_${MODE}_${TIMESTAMP}"
EXP_DIR="$PROJECT_ROOT/analysis/data/$EXP_NAME"

mkdir -p "$EXP_DIR"
BIN_FILE="$EXP_DIR/data.bin"
CSV_FILE="$EXP_DIR/data.csv"

cleanup() {
    echo "Stopping local sender..."
    pkill -2 sender || true 
}
trap cleanup EXIT

echo "========================================"
echo " Local Loopback"
echo " Mode:   $MODE"
echo " Output: $EXP_DIR"
echo "========================================"

if [[ -z "$VIRTUAL_ENV" && -f "$PROJECT_ROOT/.venv/bin/activate" ]]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
fi

echo "[Setup] Building..."
make -C "$PROJECT_ROOT/build" --no-print-directory

echo "[1/3] Starting Local Sender..."
nohup "$PROJECT_ROOT/build/sender" "$RX_IP" > /tmp/sender.log 2>&1 &
SENDER_PID=$!
echo "      Started PID $SENDER_PID"

echo "[2/3] Running Receiver... (Press Ctrl+C to stop)"
set +e 
"$PROJECT_ROOT/build/$RECEIVER_BIN_NAME" "$BIN_FILE"
set -e

echo ""
echo "[3/3] Generating Report..."
if [ -f "$BIN_FILE" ]; then
    python3 "$PROJECT_ROOT/analysis/bin_to_csv.py" "$BIN_FILE" "$CSV_FILE"
    python3 "$PROJECT_ROOT/analysis/analyze.py" "$CSV_FILE" --output "$EXP_DIR"
    echo "Done. Results in $EXP_DIR"
else
    echo "Error: No data generated."
    exit 1
fi
