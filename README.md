# net-latency-lab

**net-latency-lab** is a low-latency network benchmarking tool designed to measure packet delay distributions with high precision. It supports distributed measurement across Raspberry Pis as well as local loopback simulations for development and debugging.

## Prerequisites

* **Operating System:** Linux distribution with `AF_XDP` support.
* **Networking:** `chrony` (for hardware clock synchronization).
* **Toolchain:** `g++`, `cmake`, `make`.
* **Python:** Python 3 with `venv` support.
* **Remote Access:** Passwordless SSH key access to remote nodes (required only for distributed testing).

## Installation

### 1. Install System Dependencies
```sh
sudo apt-get install chrony
```

### 2. Clone Repository
```sh
git clone [https://github.com/etekmen13/net-latency-lab.git](https://github.com/etekmen13/net-latency-lab.git)
cd net-latency-lab
```

### 3. Configure Python Environment
The analysis scripts require a virtual environment located in the root directory.
```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Build Executable
```sh
mkdir build && cd build
cmake ..
make
```

### 5. System Tuning (Highly Recommended)
To minimize jitter, run the setup script to set the CPU scaling governor to 'performance' and isolate core 3 from interrupt requests (IRQ).

*Note: These settings persist until reboot or until `scripts/restore_env.sh` is executed. This script is not compatible with WSL/VM environments.*

```sh
sudo ./scripts/setup_env.sh
```

## Implementation Variants

The project includes three implementations to demonstrate different latency characteristics:

1.  **Baseline:** Single-threaded C++ implementation using the default Linux network stack.
2.  **Threaded:** Multi-threaded architecture utilizing a lock-free SPSC (Single Producer Single Consumer) queue to decouple the receiver and logger.
3.  **Kernel Bypass:** High-performance implementation using `AF_XDP` to zero-copy read packet data directly from the NIC ring buffer.

## Usage: Local Simulation

Use local loopback mode for development, logic verification, and debugging.

**Command Syntax:**
```sh
./scripts/run_local.sh [baseline | threaded | bypass]
```

**Note:** It is recommended to run with `sudo` to enable FIFO scheduling (real-time priority) for the receiver process.

## Usage: Distributed Benchmarking

This configuration utilizes a three-node architecture to ensure accurate measurement without observer effect.

1.  **Receiver Node:** Runs the `receiver_xxx` binary.
2.  **Sender Node:** Runs the `sender` binary.
3.  **Orchestrator Node (Host):** Triggers builds, manages SSH processes, and aggregates data.

### Network Requirements
For valid microsecond-level benchmarks, the Receiver and Sender nodes should be connected via a physical Ethernet switch. Wireless connections or routing across the public internet will introduce significant jitter, obscuring the performance differences between implementations.

### Configuration
Before executing the orchestrator script, perform the following on the **Orchestrator Node**:

1.  Ensure the repository is cloned to the **exact same path** on the Orchestrator, Receiver, and Sender nodes (e.g., `/home/pi/net-latency-lab`).
2.  Verify passwordless SSH access to both remote nodes:
    ```sh
    ssh-copy-id root@<RX_IP>
    ssh-copy-id root@<TX_IP>
    ```
3.  Update the `REMOTE_REPO_PATH` variable in `scripts/run_remote.sh` to match the directory on the remote nodes:
    ```bash
    # Inside scripts/run_remote.sh
    REMOTE_REPO_PATH="/home/pi/net-latency-lab"
    REMOTE_USER="root"
    ```
4.  (Recommended) Run `sudo ./scripts/setup_env.sh` on both remote nodes.

### Execution
Run the orchestrator script from the Host machine. It will automatically build the latest code on remote nodes, synchronize clocks, and execute the benchmark.

```sh
# Syntax: ./scripts/run_remote.sh <mode> <RX_IP> <TX_IP>

# Example
./scripts/run_remote.sh baseline 192.168.1.50 192.168.1.51
```

## Data Analysis

Benchmark results are automatically aggregated in the `analysis/data` directory. Each experiment generates a timestamped folder containing:

* **`data.bin`**: Raw binary structs captured by the receiver.
* **`data.csv`**: Decoded human-readable dataset.
* **`summary.csv`**: Statistical summary (Min, Max, Mean, Jitter, Percentiles).
* **`plot.png`**: Latency distribution histogram and timeline.
