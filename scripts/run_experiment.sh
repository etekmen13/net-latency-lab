#!/bin/bash

# Stop execution on any error (except the receiver, which we handle manually)
set -e 

# Usage check
if [[ -z "$1" || -z "$2" ]]; then
  echo "Usage: $0 <experiment_name> <receiver_ip>"
  exit 1
fi

RX_IP=$2
EXP_NAME=$1

# --- CHANGE 1: Detect Local Mode ---
IS_LOCAL=false
if [[ "$RX_IP" == "127.0.0.1" || "$RX_IP" == "localhost" ]]; then
    IS_LOCAL=true
    echo ">> Running in LOCAL SIMULATION mode (127.0.0.1)"
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")" 

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

# --- CHANGE 2: Conditional Cleanup ---
cleanup() {
    if [ "$IS_LOCAL" = true ]; then
        echo "Stopping local sender..."
        # Kill the local process named 'sender'
        pkill -2 sender || true 
    else
        echo "Stopping remote sender on pi-b..."
        ssh root@pi-b "pkill -2 sender" || true 
    fi
}
trap cleanup EXIT

echo "========================================"
echo " Experiment: $EXP_NAME"
echo " Output:     $EXP_DIR"
echo " Commit:     $GIT_HASH"
echo " Mode:       $( [ "$IS_LOCAL" = true ] && echo "Local" || echo "Remote (pi-b)" )"
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

# --- CHANGE 3: Skip Remote Sync if Local ---
if [ "$IS_LOCAL" = false ]; then
    ssh root@pi-b "$SCRIPT_DIR/sync_clocks.sh" 
fi

# Save Metadata
echo "Experiment: $EXP_NAME" > "$METADATA_FILE"
echo "Timestamp: $TIMESTAMP" >> "$METADATA_FILE"
echo "Git Commit: $GIT_HASH" >> "$METADATA_FILE"
echo "Compiler: $(g++ --version | head -n 1)" >> "$METADATA_FILE"
echo "Mode: $( [ "$IS_LOCAL" = true ] && echo "Local" || echo "Remote" )" >> "$METADATA_FILE"

# --- CHANGE 4: Fork Local vs Remote Sender ---
echo "[1/4] Starting Sender..."

if [ "$IS_LOCAL" = true ]; then
    # Local: Run directly in background (&)
    # We assume 'sender' is in the same build folder locally
    nohup "$PROJECT_ROOT/build/sender" "$RX_IP" > /tmp/sender.log 2>&1 &
    SENDER_PID=$!
    echo "      Local sender started (PID $SENDER_PID)"
else
    # Remote: Run via SSH
    ssh -f root@pi-b "nohup $PROJECT_ROOT/build/sender $RX_IP > /tmp/sender.log 2>&1 &"
    echo "      Remote sender started on pi-b"
fi

echo "[2/4] Running Receiver... (Press Ctrl+C to stop)"
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
