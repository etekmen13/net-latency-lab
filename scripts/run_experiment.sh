#!/bin/bash

set -e 

if [[ -z "$1" || -z "$2" ]]; then
  echo "Usage: $0 <mode> <receiver_ip>"
  echo "Modes: baseline | threaded"
  exit 1
fi

MODE=$1
RX_IP=$2

if [[ "$MODE" == "baseline" ]]; then
    RECEIVER_BIN_NAME="receiver_baseline"
elif [[ "$MODE" == "threaded" ]]; then
    RECEIVER_BIN_NAME="receiver_threaded"
else
    echo "Error: Mode must be 'baseline' or 'threaded'"
    exit 1
fi


if [ "$EUID" -eq 0 ]; then
  echo "WARNING: You are running this as root. This creates file permission issues."
  echo "Please run './scripts/setup_system.sh' separately as root, then run this as a normal user."
  read -p "Press Enter to continue anyway or Ctrl+C to abort..."
fi

if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]; then
    GOVERNOR=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)
    if [ "$GOVERNOR" != "performance" ]; then
        echo " [!] WARNING: CPU governor is set to '$GOVERNOR', not 'performance'."
        echo "     Results may be noisy. Run 'sudo ./scripts/setup_system.sh' to fix."
        sleep 2
    fi
fi


IS_LOCAL=false
if [[ "$RX_IP" == "127.0.0.1" || "$RX_IP" == "localhost" ]]; then
    IS_LOCAL=true
    echo ">> Running in LOCAL SIMULATION mode (127.0.0.1)"
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")" 

TIMESTAMP=$(date +"%Y-%m-%d-%H-%M-%S")
GIT_HASH=$(git rev-parse --short HEAD)
EXP_NAME="${MODE}_${TIMESTAMP}" 

VENV_PATH="$PROJECT_ROOT/.venv"
BASE_DIR="$PROJECT_ROOT/analysis/data"
EXP_DIR="$BASE_DIR/$EXP_NAME"

RECEIVER_FULL_PATH="$PROJECT_ROOT/build/$RECEIVER_BIN_NAME"
if [[ ! -f "$RECEIVER_FULL_PATH" ]]; then
    echo "Error: Binary not found at $RECEIVER_FULL_PATH"
    echo "Did you run 'make'?"
    exit 1
fi

mkdir -p "$EXP_DIR"

BIN_FILE="$EXP_DIR/data.bin"
CSV_FILE="$EXP_DIR/data.csv"
SUMMARY_FILE="$EXP_DIR/summary.csv"
PLOT_FILE="$EXP_DIR/plot.png"
METADATA_FILE="$EXP_DIR/metadata.txt"

cleanup() {
    if [ "$IS_LOCAL" = true ]; then
        echo "Stopping local sender..."
        pkill -2 sender || true 
    else
        echo "Stopping remote sender on pi-b..."
        ssh root@pi-b "pkill -2 sender" || true 
    fi
}
trap cleanup EXIT

echo "========================================"
echo " Mode:       $MODE"
echo " Binary:     $RECEIVER_BIN_NAME"
echo " Target:     $RX_IP"
echo " Output:     $EXP_DIR"
echo "========================================"

if [[ -z "$VIRTUAL_ENV" ]]; then
    if [ -f "$VENV_PATH/bin/activate" ]; then
        echo "Activating virtual environment..."
        source "$VENV_PATH/bin/activate"
    else
        echo "ERROR: Virtualenv not found at $VENV_PATH"
        exit 1
    fi
fi

echo "[Setup] Building project..."
make -C "$PROJECT_ROOT/build" --no-print-directory


echo "[Setup] Syncing clocks..."
"$SCRIPT_DIR/sync_clocks.sh" || echo "Warning: Local clock sync failed (permissions?)"

if [ "$IS_LOCAL" = false ]; then
    ssh root@pi-b "$SCRIPT_DIR/sync_clocks.sh" 
fi

echo "Experiment: $EXP_NAME" > "$METADATA_FILE"
echo "Mode: $MODE" >> "$METADATA_FILE"
echo "Timestamp: $TIMESTAMP" >> "$METADATA_FILE"
echo "Git Commit: $GIT_HASH" >> "$METADATA_FILE"
echo "Compiler: $(g++ --version | head -n 1)" >> "$METADATA_FILE"
echo "Type: $( [ "$IS_LOCAL" = true ] && echo "Local" || echo "Remote" )" >> "$METADATA_FILE"


echo "[1/4] Starting Sender..."
if [ "$IS_LOCAL" = true ]; then
    nohup "$PROJECT_ROOT/build/sender" "$RX_IP" > /tmp/sender.log 2>&1 &
    SENDER_PID=$!
    echo "      Local sender started (PID $SENDER_PID)"
else
    ssh -f root@pi-b "nohup $PROJECT_ROOT/build/sender $RX_IP > /tmp/sender.log 2>&1 &"
    echo "      Remote sender started on pi-b"
fi

echo "[2/4] Running Receiver ($MODE)... (Press Ctrl+C to stop)"
set +e 
"$RECEIVER_FULL_PATH" "$BIN_FILE"
RECEIVER_EXIT_CODE=$?
set -e


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
