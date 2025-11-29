import yaml
from datetime import datetime
import argparse
import time
import os
import sys
import subprocess
import shutil
from datetime import datetime

try:
    import paramiko
    from scp import SCPClient

    HAS_REMOTE_LIBS = True
except ImportError:
    HAS_REMOTE_LIBS = False


def pdt():
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')}]")


def recursive_chown(path, uid, gid):
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for d in dirs:
            os.chown(os.path.join(root, d), uid, gid)
        for f in files:
            os.chown(os.path.join(root, f), uid, gid)


class NodeController:

    def run(self, cmd, background=False):
        raise NotImplementedError

    def fetch_file(self, remote_path, local_path):
        raise NotImplementedError

    def close(self):
        pass


class LocalNode(NodeController):

    def run(self, cmd, background=False):
        if background:
            subprocess.Popen(cmd, shell=True, start_new_session=True)
        else:
            try:
                result = subprocess.run(
                    cmd, shell=True, check=True, capture_output=True, text=True
                )
                return result.stdout.strip()
            except subprocess.CalledProcessError as e:
                print(f"  [Local Error] Cmd: {cmd}")
                print(f"  [Local Error] Stderr: {e.stderr}")
                raise

    def fetch_file(self, remote_path, local_path):
        if os.path.exists(remote_path):
            shutil.copy2(remote_path, local_path)
        else:
            raise FileNotFoundError(f"Local file not found: {remote_path}")


class RemoteNode(NodeController):

    def __init__(self, ip, user, key_file=None):
        if not HAS_REMOTE_LIBS:
            print("Error: paramiko/scp not installed. Run 'pip install paramiko scp'")
            sys.exit(1)
        self.ip = ip
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(ip, username=user, key_filename=key_file, timeout=5)
        except Exception as e:
            print(f"Failed to connect to {user}@{ip}: {e}")
            sys.exit(1)

    def run(self, cmd, background=False):
        if background:
            transport = self.client.get_transport()
            channel = transport.open_session()
            full_cmd = f"nohup {cmd} > /dev/null 2>&1 &"
            channel.exec_command(full_cmd)
        else:
            stdin, stdout, stderr = self.client.exec_command(cmd)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                err = stderr.read().decode().strip()
                print(f"  [Remote Error] {err}")
                raise Exception(f"Remote command failed: {cmd}")
            return stdout.read().decode().strip()

    def fetch_file(self, remote_path, local_path):
        with SCPClient(self.client.get_transport()) as scp:
            scp.get(remote_path, local_path)

    def close(self):
        self.client.close()


def get_node_controller(ip, user, key_path=None):
    if ip in ["127.0.0.1", "localhost", "::1"]:
        return LocalNode()
    else:
        return RemoteNode(ip, user, key_path)


def run_analysis_pipeline(project_root, session_dir):
    print(f"\n[Analysis] Processing session: {session_dir}")
    gen_summary_script = os.path.join(project_root, "analysis", "generate_summary.py")
    plot_figs_script = os.path.join(project_root, "analysis", "plot_paper_figs.py")

    try:
        print("Generating Summary CSV...")
        subprocess.run([sys.executable, gen_summary_script, session_dir], check=True)

        summary_csv = os.path.join(session_dir, "master_summary.csv")
        if os.path.exists(summary_csv):
            print("Generating Plots...")
            subprocess.run([sys.executable, plot_figs_script, summary_csv], check=True)
            print("[Success] Analysis Complete.")
        else:
            print("[Warn] No summary generated (no data?).")
    except subprocess.CalledProcessError as e:
        print(f"[Error] Analysis script failed: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Config file not found: {args.config}")
        sys.exit(1)

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    g = cfg["global"]
    script_stat = os.stat(os.path.abspath(__file__))
    target_uid = script_stat.st_uid
    target_gid = script_stat.st_gid
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if g.get("project_root") == "." or g.get("project_root") is None:
        g["project_root"] = script_dir

    print(f"Initializing Nodes...")
    rx_node = get_node_controller(g["nodes"]["receiver_ip"], g["user"])
    tx_node = get_node_controller(g["nodes"]["sender_ip"], g["user"])

    if not args.skip_build:
        print("Building binaries...")
        build_cmd = f"make -C {g['project_root']}/build -j4"
        if isinstance(rx_node, LocalNode) and isinstance(tx_node, LocalNode):
            rx_node.run(build_cmd)
        else:
            rx_node.run(build_cmd)
            tx_node.run(build_cmd)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = os.path.join(
        g["project_root"], g["local_data_dir"], f"session_{timestamp}"
    )
    print(f"Session dir: {session_dir}")
    os.makedirs(session_dir, exist_ok=True)
    recursive_chown(session_dir, target_uid, target_gid)
    try:
        for bench in cfg["benchmarks"]:
            if not bench.get("enabled", False):
                continue

            print(f"\nCampaign: {bench['name']}")
            camp_dir = os.path.join(session_dir, bench["name"])
            os.makedirs(camp_dir, exist_ok=True)

            if not isinstance(rx_node, LocalNode):
                print("    Syncing clocks...")
                sync_cmd = f"sudo {g['project_root']}/scripts/sync_clocks.sh"
                rx_node.run(sync_cmd)
                tx_node.run(sync_cmd)

            rates = bench["sender"].get("rates_pps", [1000])
            bursts = bench["sender"].get("burst_sizes", [1])
            raw_batch = bench["receiver"].get("batch_size", 1)
            batches = raw_batch if isinstance(raw_batch, list) else [raw_batch]
            for rate in rates:
                for burst in bursts:
                    for batch in batches:
                        mode = bench["sender"]["mode"]
                        if mode == "steady" and burst > 1:
                            continue

                        print(f"[Run] {rate}pps | {mode} | Burst {burst}")

                        rx_node.run("sudo pkill -9 receiver || true")
                        tx_node.run("sudo pkill -9 sender || true")

                        run_id = (
                            f"{bench['name']}_{mode}_{rate}pps_b{burst}_batch{batch}"
                        )
                        remote_bin = f"/tmp/{run_id}.bin"

                        rx_bin = bench["receiver"]["binary"]
                        rx_cpu = bench["receiver"].get("cpu_affinity", 3)

                        rx_cmd = (
                            f"sudo {g['project_root']}/build/{rx_bin} "
                            f"--output {remote_bin} "
                            f"--port 49200 "
                            f"--cpu {rx_cpu}"
                        )

                        if "threaded" in rx_bin:
                            rx_cmd += f" --batch {batch} --worker-cpu {rx_cpu - 1}"
                        rx_node.run(rx_cmd, background=True)
                        time.sleep(0.1)

                        dur = bench["sender"]["duration_sec"]
                        tx_cmd = (
                            f"sudo {g['project_root']}/build/sender "
                            f"--ip {g['nodes']['receiver_ip']} "
                            f"--port 49197 "
                            f"--rate {rate} "
                            f"--mode {mode} "
                            f"--burst {burst} "
                            f"--duration {dur}"
                        )
                        tx_node.run(tx_cmd)
                        rx_node.run(f"sudo pkill -2 {rx_bin} || true &> /dev/null/")
                        local_bin = os.path.join(camp_dir, f"{run_id}.bin")
                        try:
                            rx_node.fetch_file(remote_bin, local_bin)

                            with open(
                                os.path.join(camp_dir, f"{run_id}_meta.yaml"), "w"
                            ) as f:
                                yaml.dump(
                                    {
                                        "campaign": bench["name"],
                                        "rate_pps": rate,
                                        "mode": mode,
                                        "burst": burst,
                                        "duration": dur,
                                        "rx_binary": rx_bin,
                                        "batch_size": batch,
                                    },
                                    f,
                                )
                        except Exception as e:
                            print(f"    [Error] Fetch failed: {e}")

    finally:
        recursive_chown(session_dir, target_uid, target_gid)
        rx_node.close()
        tx_node.close()
    run_analysis_pipeline(g["project_root"], session_dir)


if __name__ == "__main__":
    main()
