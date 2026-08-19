# DietPi Raspberry Pi Benchmark Operator Runbook

This runbook configures two Raspberry Pi 4 systems running DietPi for the distributed UDP benchmark and then runs the complete measurement and profiling campaigns.

The published methodology identifies DietPi v9.17.2, ARM64, based on Debian 13 (Trixie), as the frozen benchmark environment.

Two important prerequisites:

- Use the same DietPi release, kernel, architecture, and packages on both Pis.
- Keep DietPi's `/tmp` mounted as tmpfs so artifact I/O does not add SD-card traffic to measured runs. Verify that each Pi has at least 512 MB available in `/tmp` before launch. The harness removes a run's remote artifacts only after all required outputs are stored locally and its metadata is written; artifacts from a failed run remain under their unique `/tmp/nll_*` names for diagnosis. DietPi documents its `/tmp` tmpfs behavior in its [system configuration documentation](https://dietpi.com/docs/dietpi_tools/system_configuration/).

## 1. Assign the roles

Use this fixed layout:

| Machine | Hostname | Management | Direct Ethernet |
|---|---|---|---|
| Controller | Your laptop/workstation | Wi-Fi | None |
| Sender Pi | `nll-sender` | Wi-Fi DHCP reservation | `192.168.50.1/30` |
| Receiver Pi | `nll-receiver` | Wi-Fi DHCP reservation | `192.168.50.2/30` |

Use active cooling, stable power supplies, and a direct Cat5e/Cat6 cable between the Pis. The controller communicates with both Pis over Wi-Fi only.

## 2. Complete DietPi setup identically

Connect a monitor and keyboard temporarily, or attach one Pi at a time to the router via Ethernet.

On both Pis, complete any first-login setup. DietPi describes the initial login and setup flow in its [installation guide](https://dietpi.com/docs/install/).

Check the actual platform:

```bash
dpkg --print-architecture
uname -m
cat /etc/os-release
grep -E '^(G_DIETPI_VERSION|G_DISTRO_NAME|G_RASPBIAN)' /boot/dietpi/.version 2>/dev/null
```

You want:

```text
arm64
aarch64
DietPi v9.17.2
Debian GNU/Linux 13 (trixie)
```

The frozen benchmark environment is DietPi v9.17.2, ARM64, based on Debian 13
(Trixie). If either Pi reports a different DietPi release, Debian release, or
architecture, correct it before proceeding. Do not update either Pi after the
benchmark environment is frozen.

Set the hostnames and Wi-Fi with:

```bash
sudo dietpi-config
```

Set:

- `nll-sender` on the sender
- `nll-receiver` on the receiver
- the same Wi-Fi country and SSID on both
- Wi-Fi enabled at boot

DietPi's networking and hostname menus are documented in [DietPi-Config](https://dietpi.com/docs/dietpi_tools/system_configuration/).

After rebooting, verify:

```bash
hostname
ip -br address show wlan0
ip route show default
```

The default route should use `wlan0`.

In your router, create DHCP reservations for both Wi-Fi MAC addresses:

```bash
cat /sys/class/net/wlan0/address
```

Write down the two management IPs.

## 3. Install OpenSSH and dependencies

DietPi may initially use Dropbear. Switch both Pis to OpenSSH because the controller uses SSH and SCP for orchestration and artifact collection. OpenSSH's SCP/SFTP support is documented in DietPi's [SSH server guide](https://dietpi.com/docs/software/ssh/).

Run:

```bash
sudo dietpi-software
```

Select OpenSSH as the SSH server. Then install the project packages:

```bash
sudo apt update
sudo apt install -y \
  git \
  build-essential \
  cmake \
  libgtest-dev \
  python3-venv \
  python3-pip \
  chrony \
  ethtool \
  iproute2 \
  linux-perf \
  libcap2-bin \
  iw \
  openssh-server
```

Check the Pi utilities:

```bash
vcgencmd get_throttled
```

If `vcgencmd` is missing:

```bash
sudo apt install -y raspi-utils
```

Verify:

```bash
perf --version
vcgencmd get_throttled
```

The throttling result must be:

```text
throttled=0x0
```

Reboot both Pis:

```bash
sudo reboot
```

## 4. Configure the direct Ethernet link

First verify that Wi-Fi SSH works. Then connect `eth0` on the two Pis directly.

DietPi uses ifupdown-style configuration and supports custom files under `/etc/network/interfaces.d`; examples are available in the [official DietPi forum](https://dietpi.com/forum/t/connect-to-2-networks/3205).

On both Pis:

```bash
sudo cp -a /etc/network/interfaces /etc/network/interfaces.before-nll
sudoedit /etc/network/interfaces
```

Ensure it contains a source line such as:

```text
source /etc/network/interfaces.d/*
```

Remove or comment out any other active `eth0` configuration. Do not change the working `wlan0` section.

On the sender:

```bash
sudoedit /etc/network/interfaces.d/nll-data
```

Enter:

```text
allow-hotplug eth0
iface eth0 inet static
    address 192.168.50.1
    netmask 255.255.255.252
    mtu 1500
```

On the receiver, use:

```text
allow-hotplug eth0
iface eth0 inet static
    address 192.168.50.2
    netmask 255.255.255.252
    mtu 1500
```

There must be no gateway or DNS entry on `eth0`.

Disable IPv6 only on the benchmark interface:

```bash
sudoedit /etc/sysctl.d/90-nll-data-link.conf
```

Enter:

```text
net.ipv6.conf.eth0.disable_ipv6=1
```

Apply the settings:

```bash
sudo sysctl --system
sudo ifdown --force eth0 || true
sudo ifup eth0
```

Verify on both:

```bash
ip -4 address show eth0
ip route
ip route show default
```

The only default route must still use `wlan0`.

From the sender:

```bash
ping -I eth0 -c 5 192.168.50.2
ip route get 192.168.50.2
```

From the receiver:

```bash
ping -I eth0 -c 5 192.168.50.1
ip route get 192.168.50.1
```

Check the link on both:

```bash
sudo ethtool eth0 | grep -E 'Speed:|Duplex:|Link detected:'
sudo ethtool -k eth0
```

Required result:

```text
Speed: 1000Mb/s
Duplex: Full
Link detected: yes
```

Do not change offload settings.

If `sudo nft list ruleset` shows an active firewall, permit:

- UDP port 123 from `192.168.50.2` to the sender
- UDP benchmark port 49200 from `192.168.50.1` to the receiver

## 5. Configure Chrony

The sender will remain synchronized to normal internet time sources and serve time to the receiver over Ethernet.

On the sender:

```bash
sudo cp -a /etc/chrony/chrony.conf /etc/chrony/chrony.conf.before-nll
sudoedit /etc/chrony/chrony.conf
```

Keep its existing upstream sources and add:

```text
allow 192.168.50.0/30
```

On the receiver:

```bash
sudo cp -a /etc/chrony/chrony.conf /etc/chrony/chrony.conf.before-nll
sudoedit /etc/chrony/chrony.conf
```

Comment out existing `pool` and `server` lines, keeping the other default directives, and add:

```text
server 192.168.50.1 iburst minpoll 0 maxpoll 2 prefer
```

These directives and polling settings are defined in the official [chrony configuration reference](https://chrony-project.org/doc/4.7/chrony.conf.html).

Ensure another time daemon is not running:

```bash
sudo systemctl disable --now systemd-timesyncd 2>/dev/null || true
sudo systemctl enable --now chrony
```

Restart Chrony on the sender first, then on the receiver:

```bash
sudo systemctl restart chrony
chronyc waitsync 60 0.01
chronyc tracking
chronyc sources -v
```

On the receiver, `chronyc sources -v` should eventually mark `192.168.50.1` with `^*`, and `tracking` should report:

```text
Leap status     : Normal
```

Chrony's `tracking`, `sources`, and `waitsync` commands are documented in the [chronyc reference](https://chrony-project.org/doc/4.7/chronyc.html).

`scripts/sync_clocks.sh` re-runs exactly these read-only checks and exits
nonzero when chrony is stopped, unsynchronized, or not locked to the expected
source. It never restarts chrony or repoints it at a public pool, because either
would change the frozen environment:

```bash
# on the receiver
NLL_EXPECTED_SOURCE=192.168.50.1 ./scripts/sync_clocks.sh
# on the sender
./scripts/sync_clocks.sh
```

Cross-host latency is still software-synchronization-limited. Do not make nanosecond cross-host latency claims from this experiment.

## 6. Verify the `/tmp` tmpfs

On both Pis:

```bash
findmnt -T /tmp
df -h /tmp
df --output=avail -BM /tmp
free -h
grep -nE '[[:space:]]/tmp[[:space:]]' /etc/fstab
```

`findmnt` should identify `/tmp` as tmpfs, `df --output=avail -BM` must report at least `512M` available, and `/tmp` must have mode `1777`:

```bash
stat -c '%a %n' /tmp
```

If necessary, correct only the directory mode:

```bash
sudo chmod 1777 /tmp
```

Do not move benchmark artifacts to disk-backed storage. Only one successful run remains resident at a time: after its trace, statistics, and applicable profiling output are copied and its local run metadata is written, the harness deletes that run's trace, statistics, process logs, status files, and profiling artifact from both nodes. If execution, transfer, parsing, metadata creation, or cleanup fails, the campaign stops; artifacts that were not successfully cleaned remain in `/tmp` for diagnosis.

## 7. Configure controller SSH access

On the controller, create a key if you do not already have one:

```bash
ssh-keygen -t ed25519
```

The harness connects as `global.user` from the configuration, which is `root`
for every tracked distributed config, and it never uses `sudo` remotely: the
receiver's `setcap`, `perf`, and scheduling work all require it. Enable
`PermitRootLogin prohibit-password` in `/etc/ssh/sshd_config` on both Pis,
reload `ssh`, and copy the key to `root`:

```bash
ssh-copy-id root@SENDER_WIFI_IP
ssh-copy-id root@RECEIVER_WIFI_IP
```

Connect to each interactively once:

```bash
ssh root@SENDER_WIFI_IP true
ssh root@RECEIVER_WIFI_IP true
```

Confirm the host keys carefully. The benchmark harness rejects unknown SSH host keys, so both must already exist in the controller's `~/.ssh/known_hosts`.

Test SCP:

```bash
scp root@SENDER_WIFI_IP:/etc/hostname /tmp/nll-sender-hostname
scp root@RECEIVER_WIFI_IP:/etc/hostname /tmp/nll-receiver-hostname
```

## 8. Freeze the benchmark commit

On the controller, review the repository's existing uncommitted changes:

```bash
cd /home/etekmen13/projects/net-latency-lab
git status --short
git diff --check
git diff --stat
```

Before committing, verify that the methodology identifies the frozen environment exactly:

```text
DietPi v9.17.2, ARM64, based on Debian 13 (Trixie)
```


Run the local tests, then review and commit intentionally:

```bash
cmake --preset dev
cmake --build --preset dev
ctest --preset dev
.venv/bin/python -m pytest -q
```

After reviewing every addition and deletion:

```bash
git add <reviewed-files>
git commit -m "Prepare reproducible Raspberry Pi benchmark"
git push
git rev-parse HEAD
```

Save the resulting full SHA as `BENCH_COMMIT`. Do not commit physical results into this checkpoint.

## 9. Clone and test the benchmark commit on both Pis

Run on each Pi, substituting your repository URL and commit:

```bash
cd /root
git clone <REPO_URL> net-latency-lab
cd net-latency-lab
git checkout --detach <BENCH_COMMIT>

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

cmake --preset pi4-release
cmake --build --preset pi4-release -j2
ctest --preset pi4-release
.venv/bin/python -m pytest -q
```

Run ThreadSanitizer separately:

```bash
cmake --preset tsan
cmake --build --preset tsan -j2
ctest --preset tsan
```

Finally verify that both checkouts remain clean and exact:

```bash
git rev-parse HEAD
git status --porcelain
```

The second command must print nothing.

Reboot both Pis after the tests and wait for Chrony to synchronize again.

## 10. Apply benchmark tuning

First identify the actual IRQ labels on each Pi:

```bash
sudo ethtool -i eth0
grep -Ei 'bcmgenet|brcmfmac|eth0|wlan|mmc' /proc/interrupts
```

Label conventions differ by kernel, so read the output rather than trusting either
convention. On the Raspberry Pi Foundation kernel shipped with DietPi v9.17.2
(6.18.x `+rpt-rpi-v8`) the NIC lines are labelled **`eth0`**, not `bcmgenet`, and
there is no `wlan` line at all: the Wi-Fi adapter is SDIO-attached and shares the
`mmc1, mmc0` interrupt with the SD card. Passing `'bcmgenet|brcmfmac'` on such a
system matches nothing, and `setup_env.sh` aborts with "no IRQ was steered".

`setup_env.sh` always additionally steers anything matching
`wlan|mmc|brcm` (overridable via `HOUSEKEEPING_IRQ_PATTERN`), so the management and
storage interrupts are kept off the pinned measurement cores whichever label
convention your kernel uses. That line otherwise keeps a `0-3` affinity mask, i.e.
it is permitted to fire on the receiver and worker cores.

On the receiver:

```bash
cd /root/net-latency-lab

sudo env \
  PROJECT_ROOT=/root/net-latency-lab \
  BUILD_SUBDIR=pi4-release \
  ./scripts/setup_env.sh \
  receiver \
  'eth0' \
  /var/tmp/nll-tuning-receiver
```

On the sender:

```bash
cd /root/net-latency-lab

sudo env \
  PROJECT_ROOT=/root/net-latency-lab \
  BUILD_SUBDIR=pi4-release \
  ./scripts/setup_env.sh \
  sender \
  'eth0' \
  /var/tmp/nll-tuning-sender
```

The quoted pattern matches the NIC lines only; replace it with whatever your own `/proc/interrupts` prints. On kernels that name IRQs after the driver rather than the interface, pass `'bcmgenet'` instead. The script reports how many lines it steered and fails loudly if that count is zero, so a mismatched pattern cannot pass silently.

Verify on both:

```bash
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
sysctl net.core.rmem_max net.core.wmem_max
vcgencmd get_throttled
iw dev wlan0 get power_save
```

On the receiver:

```bash
getcap build/pi4-release/receiver_*
```

The governors must be `performance`, throttling must be `0x0`, and receiver binaries must have `cap_sys_nice=ep`.

On the receiver, exercise the privileged threaded startup and shutdown path
repeatedly against the capability-bearing release binary:

```bash
for repetition in $(seq 1 20); do
  NLL_THREADED_RECEIVER_BINARY=build/pi4-release/receiver_threaded \
    .venv/bin/python -m pytest -q tests/test_loopback.py \
    -k threaded_fifo_affinity_lifecycle
done
```

Every repetition must pass; a skip means the release binary did not receive
`CAP_SYS_NICE` and must be corrected before preflight.

Inspect the IRQ snapshot and confirm the relevant IRQs were found:

```bash
find /var/tmp/nll-tuning-receiver/irq -maxdepth 1 -type f
```

Do not continue if that directory is empty.

Prevent package-update timers from waking during the run:

```bash
sudo systemctl stop apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl is-active apt-daily.service apt-daily-upgrade.service
```

Both services should be inactive. Leave Chrony running.

## 11. Create the distributed configuration

On the controller:

```bash
cd /home/etekmen13/projects/net-latency-lab
mkdir -p results/raw
cp config_distributed.yaml results/raw/config-dietpi.yaml
```

Edit the ignored working copy:

```bash
nano results/raw/config-dietpi.yaml
```

Set exactly these keys; the harness reads no other shape, and any extra
top-level block is silently ignored:

```yaml
global:
  benchmark_commit: "<BENCH_COMMIT>"
  remote_project_root: "/root/net-latency-lab"
  user: "root"
  nodes:
    receiver:
      management_host: "<RECEIVER_WIFI_IP_OR_HOSTNAME>"
      benchmark_ip: "192.168.50.2"
      interface: "eth0"
    sender:
      management_host: "<SENDER_WIFI_IP_OR_HOSTNAME>"
      benchmark_ip: "192.168.50.1"
      interface: "eth0"
```

`management_host` may be a Wi-Fi IP or an `~/.ssh/config` alias such as
`nll-sender`/`nll-receiver`; the tracked configs use the aliases. `benchmark_ip`
is always the direct `/30` address and is never used for SSH.

Leave these values unchanged:

- shuffle seed `477`
- 4 MiB socket buffers
- CPU assignments
- all rate grids
- five repetitions
- 30-second durations
- batch sizes
- loss thresholds
- one-second drain
- Ethernet benchmark addresses

Install the controller environment if necessary:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## 12. Run preflight

From the controller:

```bash
./run_lab.sh \
  --config results/raw/config-dietpi.yaml \
  --preflight \
  --preflight-report results/raw/preflight-dietpi.json
```

Do not bypass a failure. Correct the cause and rerun preflight.

Preflight should confirm:

- clean exact Git commit
- tests pass on both Pis
- 1 Gb/s full-duplex link
- Chrony synchronized
- no thermal throttling
- performance governor
- socket maxima
- `perf` present
- scheduling capabilities present
- complete machine metadata

The TSan test is not part of preflight; that is why it was run manually earlier.

## 13. Qualify the sender and run the pilot

The frozen protocol (`docs/experiment_plan.md`, campaigns 1 and 2) requires two
separate non-claim sessions before any measurement campaign. Both use the
tracked configs directly, not the `results/raw/` working copy, and both must run
against the candidate `benchmark_commit` that is checked out on the Pis.

```bash
cd /home/etekmen13/projects/net-latency-lab
./run_lab.sh --config config_sender_qualification.yaml --preflight
./run_lab.sh --config config_sender_qualification.yaml
```

Qualification passes only when every repetition of every rate reaches 99-101% of
the requested PPS, reconciles sender and receiver accounting, leaves every kernel
and NIC counter unchanged, holds 1 Gb/s full duplex with `throttled=0x0`, and
keeps the 1 ms pacing p1/p99 inside 90-110% after the 100 ms edge exclusion.
Each qualification benchmark therefore needs `pacing_trace: true`; without it the
harness records `pacing trace analysis is missing` and fails every repetition.

Apply the batch-window escalation exactly as written. Start at
`sender.batch_window_us: 10`. If any repetition fails, **preserve that session
directory**, edit `config_sender_qualification.yaml` to the next window, rename
the benchmark to match (`sender_qualification_window25`, `_window50`,
`_window100`), and run a completely fresh session. Escalate 10 -> 25 -> 50 -> 100
microseconds and stop at the first window whose complete grid passes. Only if all
four fail should you consider two sender workers (`threads: 2` with explicit
`cpus`), and only after that an `external_generator` topology.

Then run the non-claim pilot with the winning sender block copied into
`config_pilot.yaml`:

```bash
./run_lab.sh --config config_pilot.yaml
```

The pilot must expose a throughput/loss knee or show that an implementation
sustains 950k PPS. Neither the qualification nor the pilot session may be used
for a claim.

Freeze the passing sender and protocol in a **new** commit, push it, update
`benchmark_commit` in `config_distributed.yaml` (and in
`results/raw/config-dietpi.yaml`) and in `docs/checkpoints.txt`, redeploy that
commit to both Pis, then rerun preflight before section 14.

## 14. Run the measurement campaign

Prepare the controller:

- Connect it to mains power.
- Disable sleep and automatic reboot.
- Keep it on the same management Wi-Fi.
- Do not rebuild or change any setting after preflight.
- Close all manually opened SSH sessions.
- Do not use either Pi interactively during the campaign.

Use `tmux` so a terminal disconnect does not terminate the controller:

```bash
tmux new -s nll
cd /home/etekmen13/projects/net-latency-lab
./run_lab.sh \
  --config results/raw/config-dietpi.yaml \
  --skip-build
```

The harness will perform:

1. Sender qualification.
2. Freeze the eligible sender-rate grid.
3. Raw throughput campaign.
4. 10 µs workload campaign.
5. Summary generation.

Plan for roughly 12–16 hours. The exact duration depends on the eligible raw-throughput grid and SSH setup overhead.

During the run:

- Do not poll either Pi manually.
- Do not transfer files.
- Do not move cables.
- Do not update packages.
- Do not change cooling or tuning.
- Do not restore the environment after measurements—the profile runs need the same frozen setup.

Record the session directory printed at completion, for example:

```bash
MEASUREMENT_SESSION=/home/etekmen13/projects/net-latency-lab/results/sessions/session_<timestamp>
```

If a run is invalid, do not delete it or substitute one favorable repetition. Preserve it and rerun according to the predefined protocol.

## 15. Run profiling

For the frozen DietPi campaign, the controller can perform the complete
deployment, test, smoke, preflight, profile, validation, and summary sequence
with one synchronous command:

```bash
./scripts/run_profile_phase.sh
```

The command refuses dirty controller or Pi checkouts and stale benchmark
processes. It deploys the exact `benchmark_commit` shared by the measurement
and profile configs, preserves timestamped logs under `results/profile-phase/`,
and prints the validated session path on success. It does not commit, push,
clean either checkout, kill stale processes, retry tuples, or accept a receiver
shutdown status other than zero. To check local configuration and frozen-commit
invariants without contacting either Pi, use:

```bash
./scripts/run_profile_phase.sh --dry-run
```

The manual commands below remain useful for diagnosis and individual reruns.

Before starting the controller campaign, run the lifecycle smoke locally on the
receiver Pi (never while a benchmark interval is active):

```bash
# Use the path configured as global.remote_project_root. For the active config:
cd /root/net-latency-lab
BUILD_SUBDIR=pi4-release ./scripts/profile_shutdown_smoke.sh
```

The smoke must print status 0 for both `perf stat` and `perf record`. It rejects
missing/empty receiver stats, traces, or perf artifacts, requires `perf report`
to succeed for the record capture, and checks that the wrapper and receiver
have both exited. The script sends SIGINT only to the PID published by the
receiver exec shim; it then waits for `perf` to finalize naturally.

Prepare the mechanical profile configuration:

```bash
cd /home/etekmen13/projects/net-latency-lab

.venv/bin/python analysis/profile_tools.py prepare \
  "$MEASUREMENT_SESSION" \
  results/raw/config-dietpi.yaml \
  results/raw/profile-dietpi.yaml
```

Inspect the generated plan:

```bash
less results/raw/profile-dietpi.plan.json
```

Do not manually replace the selected batches or profiling rate.

Run the profile campaign:

```bash
./run_lab.sh \
  --config results/raw/profile-dietpi.yaml \
  --skip-build
```

Save its printed session directory:

```bash
PROFILE_SESSION=/home/etekmen13/projects/net-latency-lab/results/sessions/session_<profile-timestamp>
```

Generate the profile summary:

```bash
.venv/bin/python analysis/profile_tools.py summarize \
  "$PROFILE_SESSION"
```

Profile runs are explanatory evidence only; do not include them in throughput or latency claims.

For profiled runs, metadata `processes.receiver_pid` is the actual receiver PID
and `processes.profile_wrapper_pid` is the `perf` PID. Workload PID publication
and all other process discovery finish before the sender is launched, so the
timed sender-plus-drain interval remains control-plane silent.

## 16. Restore both Pis

Only after measurement and profiling have completed and artifacts are present on the controller.

On the receiver:

```bash
cd /root/net-latency-lab
sudo ./scripts/restore_env.sh /var/tmp/nll-tuning-receiver
sudo systemctl start apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
```

On the sender:

```bash
cd /root/net-latency-lab
sudo ./scripts/restore_env.sh /var/tmp/nll-tuning-sender
sudo systemctl start apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
```

Reboot and verify normal operation:

```bash
sudo reboot
```

Keep the tuning snapshot directories until the results are published.

## 17. Produce the publication artifacts

On the controller:

```bash
cd /home/etekmen13/projects/net-latency-lab

.venv/bin/python analysis/publish_results.py \
  "$MEASUREMENT_SESSION" \
  "$PROFILE_SESSION" \
  results/pi4-dietpi-<YYYY-MM-DD>
```

Review:

- cleaned summary CSVs
- `claim_evidence.csv`
- both generated figures
- profile summary and report extracts
- environment metadata
- checksum manifest
- invalid-run reports

Only publish a numerical claim when `claim_evidence.csv` supports it and the profiling data supports its proposed mechanism. Commit these results in a separate results-publication commit, not the benchmark-code commit.
