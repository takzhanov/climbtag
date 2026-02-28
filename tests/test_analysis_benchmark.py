import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_analysis_benchmark():
    if os.environ.get("RUN_ANALYSIS_BENCHMARK") != "1":
        pytest.skip("Set RUN_ANALYSIS_BENCHMARK=1 to run the heavy analysis benchmark.")

    case_name = os.environ.get("ANALYSIS_BENCHMARK_CASE", "short")
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "analysis_benchmark.py"),
        "--case",
        case_name,
    ]
    completed = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(
            "analysis benchmark failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
