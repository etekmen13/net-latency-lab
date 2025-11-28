# net-latency-lab

**net-latency-lab** is a low-latency network benchmarking tool designed to measure packet delay distributions with high precision. It supports distribured measurement across Raspberry Pis as well as local loopback simulations for development and debugging.

## Setup

### Prerequisites
1. Linux distro with `AF_XDP` support.
2. Linux networking tools: `chrony`
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
3. Set up CMake
```sh
mkdir build && cd build
cmake ..
```

4. Install Linux dependencies
```sh
sudo apt-get install chrony
```
## Usage
There are three versions of the program:
1. Baseline: Single threaded C++ using the default Linux network stack.
2. Threaded: One receiver thread, one logger thread, using a lock-free SPSC queue.
3. Kernel Bypass: Uses `AF_XDP` to Zero-Copy read packet data from the NIC's ring buffer.

To run each:
```
cd scripts
./run_experiment.sh [baseline | threaded | bypass] [RX_IP] [TX_IP]
```

Replace `RX_IP` with the receiver ip address (or localhost for local loopback)
Replace `TX_IP` with sender ip address (or leave blank if sending from localhost)

