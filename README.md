# net-latency-lab

**net-latency-lab** is a low-latency network benchmarking tool designed to measure packet delay distributions with high precision. It supports distribured measurement across Raspberry Pis as well as local loopback simulations for development and debugging.

# Setup

## Prerequisites
1. C++ toolchain: `g++`, `cmake`, `make`
2. Python 3 with `venv` support
3. SSH access (for remote only): Passwordless SSH key access to the remote node(s).

## Installation

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
3. 
