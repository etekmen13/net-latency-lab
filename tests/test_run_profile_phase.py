from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/run_profile_phase.sh"


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def make_driver_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "controller"
    origin = tmp_path / "origin.git"
    (repo / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(origin)], check=True,
                   capture_output=True)
    run(["git", "init", "-b", "main"], repo)
    run(["git", "config", "user.name", "Profile Test"], repo)
    run(["git", "config", "user.email", "profile-test@example.invalid"], repo)
    (repo / "benchmark.txt").write_text("frozen\n")
    run(["git", "add", "benchmark.txt"], repo)
    run(["git", "commit", "-m", "frozen benchmark"], repo)
    frozen_sha = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()

    shutil.copy2(SCRIPT, repo / "scripts/run_profile_phase.sh")
    os.chmod(repo / "scripts/run_profile_phase.sh", 0o755)
    config = {
        "global": {
            "topology": "distributed_ethernet",
            "benchmark_commit": frozen_sha,
            "user": "root",
            "remote_project_root": "/root/net-latency-lab",
            "nodes": {
                "receiver": {"management_host": "192.0.2.10"},
                "sender": {"management_host": "192.0.2.11"},
            },
            "runtime": {"build_preset": "pi4-release"},
        },
        "benchmarks": [],
    }
    preflight = repo / "preflight.yaml"
    profile = repo / "profile.yaml"
    preflight.write_text(yaml.safe_dump(config))
    profile.write_text(yaml.safe_dump(config))
    run(["git", "add", "scripts/run_profile_phase.sh", "preflight.yaml", "profile.yaml"], repo)
    run(["git", "commit", "-m", "controller tooling"], repo)
    run(["git", "remote", "add", "origin", str(origin)], repo)
    run(["git", "push", "-u", "origin", "main"], repo)
    return repo, preflight, profile


def invoke(repo: Path, preflight: Path, profile: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["NLL_PYTHON"] = sys.executable
    return subprocess.run([
        str(repo / "scripts/run_profile_phase.sh"), "--dry-run",
        "--preflight-config", str(preflight),
        "--profile-config", str(profile),
    ], cwd=repo, env=environment, capture_output=True, text=True)


def test_profile_driver_dry_run_validates_without_ssh(tmp_path: Path):
    repo, preflight, profile = make_driver_repo(tmp_path)
    result = invoke(repo, preflight, profile)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Dry run passed local validation" in result.stdout
    assert "192.0.2.10" in result.stdout


def test_profile_driver_dry_run_rejects_mismatched_sha_and_topology(tmp_path: Path):
    repo, preflight, profile = make_driver_repo(tmp_path)
    config = yaml.safe_load(profile.read_text())
    config["global"]["benchmark_commit"] = "1" * 40
    config["global"]["topology"] = "loopback"
    profile.write_text(yaml.safe_dump(config))
    run(["git", "add", "profile.yaml"], repo)
    run(["git", "commit", "-m", "mismatch fixture"], repo)
    run(["git", "push"], repo)

    result = invoke(repo, preflight, profile)
    assert result.returncode != 0
    assert "Configs disagree on: benchmark_commit, topology" in result.stderr


def test_profile_driver_dry_run_rejects_dirty_tracked_tree(tmp_path: Path):
    repo, preflight, profile = make_driver_repo(tmp_path)
    (repo / "benchmark.txt").write_text("dirty\n")
    result = invoke(repo, preflight, profile)
    assert result.returncode != 0
    assert "Controller tracked worktree is dirty" in result.stderr
