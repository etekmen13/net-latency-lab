from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "analysis"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ANALYSIS) not in sys.path:
    sys.path.insert(0, str(ANALYSIS))


@pytest.fixture(scope="session")
def binaries(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    build = tmp_path_factory.mktemp("cmake-build")
    subprocess.run(["cmake", "-S", str(ROOT), "-B", str(build),
                    "-DCMAKE_BUILD_TYPE=Release"], check=True)
    subprocess.run(["cmake", "--build", str(build), "-j2"], check=True)
    return {name: build / name for name in (
        "receiver_baseline", "receiver_batched", "receiver_threaded", "sender")}
