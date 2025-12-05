# net-latency-lab

**net-latency-lab** is a low-latency network benchmarking tool designed to measure packet delay distributions with high precision. It supports distributed measurement across Raspberry Pis as well as local loopback simulations for development and debugging.

## Prerequisites

* **Operating System:** Linux distribution with `AF_XDP` support.
* **Networking:** `chrony` (for hardware clock synchronization).
* **Toolchain:** `g++13`, `c++23`, `cmake`, `make`.
* **Python:** Python 3 with `venv` support.
* **Remote Access:** Passwordless SSH key access to remote nodes (required only for distributed testing).

## Installation

### 1. Install System Dependencies
```sh
sudo apt-get install chrony
````

### 2\. Clone Repository

```sh
git clone https://github.com/etekmen13/net-latency-lab.git
cd net-latency-lab
```

### 3\. Configure Python Environment

The analysis scripts and orchestrator require a virtual environment.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4\. Build Executable

```sh
mkdir build && cd build
cmake ..
make
```

### 5\. System Tuning (Highly Recommended)

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

## Usage

The lab is orchestrated by `run_lab.sh`, which wraps the Python automation logic. It handles building code on remote nodes, synchronizing clocks, running experiments, and aggregating results.

### Local Simulation (Loopback)

Use local loopback mode for development, logic verification, and debugging.

1.  **Execute the Lab:**
    ```sh
    ./run_lab.sh --config config_local.yaml
    ```
    *Note: It is recommended to run as a user with sudo privileges (or configure passwordless sudo) to enable FIFO scheduling (real-time priority) for the receiver process.*

### Distributed Benchmarking

This configuration utilizes a three-node architecture to ensure accurate measurement without observer effect.

1.  **Receiver Node:** Runs the `receiver_xxx` binary.
2.  **Sender Node:** Runs the `sender` binary.
3.  **Orchestrator Node (Host):** Triggers builds, manages SSH processes, and aggregates data.

#### Network Requirements

For valid microsecond-level benchmarks, the Receiver and Sender nodes should be connected via a physical Ethernet switch. Wireless connections will introduce significant jitter.

#### Configuration

Before executing the orchestrator, perform the following on the **Orchestrator Node**:

1.  **Clone Everywhere:** Ensure the repository is cloned to the **exact same path** on the Orchestrator, Receiver, and Sender nodes (e.g., `/home/pi/net-latency-lab`).
2.  **SSH Access:** Verify passwordless SSH access from the Orchestrator to both remote nodes:
    ```sh
    ssh-copy-id root@<RX_IP>
    ssh-copy-id root@<TX_IP>
    ```
3.  **Update Config:** Edit `config.yaml` to match your network topology:
    ```yaml
    global:
      remote_project_root: "/home/pi/net-latency-lab"
      user: "root"
      nodes:
        receiver_ip: "192.168.1.10"
        sender_ip: "192.168.1.11"
        receiver_iface: "eth0"
    ```
4.  **(Recommended)** Run `sudo ./scripts/setup_env.sh` on both remote nodes.

#### Execution

Run the lab using the default configuration:

```sh
./run_lab.sh
```

## Data Analysis

Benchmark results are automatically aggregated in the `analysis/data` directory. Each experiment generates a timestamped folder containing:

  * **`master_summary.csv`**: Statistical summary (Min, Max, Mean, Jitter, Percentiles) for all runs in the session.
  * **`*.bin`**: Raw binary structs captured by the receiver.
  * **Plots**: High-quality figures generated automatically, including:
      * Latency vs. Throughput curves.
      * Jitter stability analysis.
      * Tail latency CDFs.

