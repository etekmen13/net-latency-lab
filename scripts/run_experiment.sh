#!/bin/bash

# Stop execution on any error (except the receiver, which we handle manually)
set -e 

# Usage check
if [ -z "$1" ]; then
  echo "Usage: $0 <experiment_name>"
  exit 1
fi

RX_IP="192.168.1.10"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")" 

EXP_NAME=$1
TIMESTAMP=$(date +"%Y-%m-%d-%H-%M-%S")
GIT_HASH=$(git rev-parse --short HEAD)

VENV_PATH="$PROJECT_ROOT/.venv"
BASE_DIR="$PROJECT_ROOT/analysis/data"
EXP_DIR="$BASE_DIR/$EXP_NAME/$TIMESTAMP"

# Create output directory
mkdir -p "$EXP_DIR"

BIN_FILE="$EXP_DIR/data.bin"
CSV_FILE="$EXP_DIR/data.csv"
SUMMARY_FILE="$EXP_DIR/summary.csv"
PLOT_FILE="$EXP_DIR/plot.png"
METADATA_FILE="$EXP_DIR/metadata.txt"

# --- IMPROVEMENT 2: Cleanup Trap ---
# If the script is killed, ensure we kill the remote sender
cleanup() {
    echo "Stopping remote sender on pi-b..."
    # Sends SIGINT (Ctrl+C) to the sender process named 'sender_binary'
    ssh root@pi-b "pkill -2 sender" || true 
}
trap cleanup EXIT

echo "========================================"
echo " Experiment: $EXP_NAME"
echo " Output:     $EXP_DIR"
echo " Commit:     $GIT_HASH"
echo "========================================"

# Initialization
if [[ -z "$VIRTUAL_ENV" ]]; then
    if [ -f "$VENV_PATH/bin/activate" ]; then
        echo "Activating virtual environment..."
        source "$VENV_PATH/bin/activate"
    else
        echo "ERROR: Virtualenv not found at $VENV_PATH"
        exit 1
    fi
fi

# Build
echo "[Setup] Building project..."
make -C "$PROJECT_ROOT/build" --no-print-directory

# Hardware Setup
echo "[Setup] Configuring environment..."
"$SCRIPT_DIR/setup_env.sh"

echo "[Setup] Syncing clocks..."
"$SCRIPT_DIR/sync_clocks.sh"
ssh root@pi-b "$SCRIPT_DIR/sync_clocks.sh" 

# Save Metadata
echo "Experiment: $EXP_NAME" > "$METADATA_FILE"
echo "Timestamp: $TIMESTAMP" >> "$METADATA_FILE"
echo "Git Commit: $GIT_HASH" >> "$METADATA_FILE"
echo "Compiler: $(g++ --version | head -n 1)" >> "$METADATA_FILE"

# --- IMPROVEMENT 3: Auto-Start Sender ---
echo "[1/4] Starting Remote Sender (pi-b)..."
# Adjust the path to where the binary lives on Pi-B
ssh -f root@pi-b "nohup $PROJECT_ROOT/build/sender $RX_IP > /tmp/sender.log 2>&1 &"
echo "[2/4] Running Receiver... (Press Ctrl+C to stop)"
# We allow this to fail (Ctrl+C returns non-zero usually) so we turn off set -e temporarily
set +e 
"$PROJECT_ROOT/build/receiver" "$BIN_FILE"
RECEIVER_EXIT_CODE=$?
set -e

# The trap will fire here automatically to kill the sender

echo ""
echo "[3/4] Converting Binary to CSV..."
if [ -f "$BIN_FILE" ]; then
    python3 "$PROJECT_ROOT/analysis/bin_to_csv.py" "$BIN_FILE" "$CSV_FILE"
else
    echo "Error: No binary file generated."
    exit 1
fi

echo "[4/4] Generating Plots and Summary..."
if [ -f "$CSV_FILE" ]; then
    python3 "$PROJECT_ROOT/analysis/receiver_delay.py" "$CSV_FILE" --output "$EXP_DIR"
else
    echo "Error: CSV conversion failed."
    exit 1
fi

echo "Done. Results in $EXP_DIR"
