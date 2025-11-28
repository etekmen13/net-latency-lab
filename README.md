# net-latency-lab

**net-latency-lab** is a low-latency network benchmarking tool designed to measure packet delay distributions with high precision. It supports distribured measurement across Raspberry Pis as well as local loopback simulations for development and debugging.

## Setup

### Prerequisites
1. Linux distro with `AF_XDP` support.
2. Linux networking tool: `chrony` (see Installation)
3. C++ toolchain: `g++`, `cmake`, `make`
4. Python 3 with `venv` support
5. SSH access (for remote only): Passwordless SSH key access to the remote node(s).

### Installation

1. Clone the repository:
```sh
git clone https://github.com/etekmen13/net-latency-lab.git
cd net-latency-lab
```
2. Set up the Python Environment
The script expects a virtual environment `.venv` in the root directory
```sh 
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
3. Build executable
```sh
mkdir build && cd build
cmake ..
make
```

4. Install Linux dependencies
```sh
sudo apt-get install chrony
```

5. Run setup script (optional, recommended)
This sets the cpu scaling governor to 'performance', and prevents interrupt requests from core 3.
These settings will persist until your reboot your computer, or by running `restore_env.sh` in scripts.
Note: This will not work on WSL/VMs.
```sh
cd scripts
sudo ./setup_env.sh
```

## Local Usage

### Versions
There are three versions of the program:
1. Baseline: Single threaded C++ using the default Linux network stack.
2. Threaded: One receiver thread, one logger thread, using a lock-free SPSC queue.
3. Kernel Bypass: Uses `AF_XDP` to Zero-Copy read packet data from the NIC's ring buffer.

To test each:

```
./scripts/run_local.sh [baseline | threaded | bypass]
```

## Remote Usage

This mode uses 3-node architecture

1. Receiver node: runs the `receiver_xxx` binary
2. Sender node: runs the `sender` binary
3. Orchestrator node (your computer): triggers builds, starts processes via SSH, and aggregates data.

### Network Requirements

This lab was intended for LAN only. For valid microsecond-level benchmarks, the receiver and sender nodes should be connected via physical Ethernet switch. Using Wi-Fi or routing across the internet will introduce jitter that obscures code performance.

### Remote Configuration

Before running the orchestrator script (`run_remote.sh`), ensure the following:

- The repository must be cloned to the **exact same path** on both remote nodes (e.g. `home/pi/net-latency-lab`)

- You must have passwordless SSH access from the Orchestrator to both remote nodes:
  ```sh
  ssh-copy-id root@<RX_IP>
  ssh-copy-id root@<TX_IP>
  ```
- Open `scripts/run_remote.sh` and update the `REMOTE_REPO_PATH` variable to match the location on the remote nodes:
  ```sh
  # Inside scripts/run_remote.sh
  REMOTE_REPO_PATH="/home/pi/net-latency-lab"
  REMOTE_USER="root"
  ```
- (Optional, recommended) Run `sudo ./scripts/setup_env.sh` on both remote nodes to improve performance and benchmark quality. 

### Execution

Run the orchestrator script from your computer. It will SSH into the remote nodes, build the latest code, sync clocks, and execute the benchmark.

```sh
# Syntax: ./scripts/run_remote.sh <mode> <RX_IP> <TX_IP>

# Example
./scripts/run_remote.sh baseline 192.168.1.50 192.168.1.51
```

## Data Analysis

Benchmark results are stored in `analysis/data`. Each benchmark is labeled `[local/remote]_[mode]_[timestamp]`, and contain the following:
```sh
- data.bin    # binary structs written by the receiver program
- data.csv    # human-readable data from bin_to_csv.py
- summary.csv # summary stats
- plot.png    # histogram of full data
```
