#!/bin/bash

set -e

REMOTE_REPO_PATH="/root/net-latency-lab"
REMOTE_USER="root"

if [[ -z "$1" || -z "$2" || -z "$3" ]]; then
  echo "Usage: $0 <mode> <RX_IP> <TX_IP>"
  echo "Example: $0 baseline 192.168.1.50 192.168.1.51"
  exit 1
fi

MODE=$1
RX_HOST=$2
TX_HOST=$3

if [[ "$MODE" == "baseline" ]]; then
    BIN_NAME="receiver_baseline"
elif [[ "$MODE" == "threaded" ]]; then
    BIN_NAME="receiver_threaded"
else
    echo "Error: Mode must be 'baseline' or 'threaded'"
    exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TIMESTAMP=$(date +"%Y-%m-%d-%H-%M-%S")
EXP_DIR="$PROJECT_ROOT/analysis/data/${MODE}_${TIMESTAMP}"
mkdir -p "$EXP_DIR"

REMOTE_BUILD_DIR="$REMOTE_REPO_PATH/build"
REMOTE_BIN_OUT="$REMOTE_REPO_PATH/analysis/data/remote_run.bin"

echo "========================================"
echo " REMOTE ORCHESTRATION"
echo " Mode:     $MODE"
echo " Receiver: $RX_HOST"
echo " Sender:   $TX_HOST"
echo "========================================"

cleanup() {
    echo ""
    echo "[Teardown] Stopping remote processes..."
    
    ssh "$REMOTE_USER@$TX_HOST" "pkill -2 sender" || true

    ssh "$REMOTE_USER@$RX_HOST" "pkill -2 $BIN_NAME" || true

    echo "[Data] Downloading logs from Receiver..."
    if scp "$REMOTE_USER@$RX_HOST:$REMOTE_BIN_OUT" "$EXP_DIR/data.bin"; then
        echo "       Download successful."
        
        echo "[Analysis] Generating plots on Laptop..."
        if [[ -z "$VIRTUAL_ENV" && -f "$PROJECT_ROOT/.venv/bin/activate" ]]; then
            source "$PROJECT_ROOT/.venv/bin/activate"
        fi
        
        python3 "$PROJECT_ROOT/analysis/bin_to_csv.py" "$EXP_DIR/data.bin" "$EXP_DIR/data.csv"
        python3 "$PROJECT_ROOT/analysis/analyze.py" "$EXP_DIR/data.csv" --output "$EXP_DIR"
        
        echo "Done! Report generated in: $EXP_DIR"
    else
        echo "ERROR: Failed to retrieve data.bin from receiver."
    fi
}
trap cleanup SIGINT EXIT


echo "[1/3] Preparation..."
ssh "$REMOTE_USER@$RX_HOST" "make -C $REMOTE_BUILD_DIR $BIN_NAME --no-print-directory"
ssh "$REMOTE_USER@$TX_HOST" "make -C $REMOTE_BUILD_DIR sender --no-print-directory"

ssh "$REMOTE_USER@$RX_HOST" "$REMOTE_REPO_PATH/scripts/sync_clocks.sh"
ssh "$REMOTE_USER@$TX_HOST" "$REMOTE_REPO_PATH/scripts/sync_clocks.sh"

echo "[2/3] Starting Measurement..."
ssh -f "$REMOTE_USER@$RX_HOST" \
    "nohup $REMOTE_BUILD_DIR/$BIN_NAME $REMOTE_BIN_OUT > /tmp/receiver.log 2>&1 &"

sleep 1

ssh -f "$REMOTE_USER@$TX_HOST" \
    "nohup $REMOTE_BUILD_DIR/sender $RX_HOST > /tmp/sender.log 2>&1 &"

echo "[3/3] Experiment Running. Press Ctrl+C to stop and analyze."
while true; do
    sleep 1
done
