from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from app.processing import run_protocol_analysis  # noqa: E402


# Keep this aligned with app.main.DEFAULT_SETTINGS when benchmarking the default path.
DEFAULT_SETTINGS = {
    "frame_interval_sec": 3,
    "conf_limit": 3,
    "session_timeout_sec": 360,
    "phantom_timeout_sec": 60,
}

MODEL_PATH = PROJECT_ROOT / "models" / "yolov8n.pt"
FIXTURES_DIR = PROJECT_ROOT / "benchmarks" / "fixtures"
BASELINES_DIR = PROJECT_ROOT / "benchmarks" / "baselines"
RUNS_DIR = PROJECT_ROOT / "benchmarks" / "runs"
INPUT_VIDEOS_DIR = PROJECT_ROOT / "input" / "videos"

CASES = {
    "short": {
        "video": INPUT_VIDEOS_DIR / "short_video.mp4",
        "protocol": FIXTURES_DIR / "protocol.csv",
        "baseline": BASELINES_DIR / "short_default.json",
    },
    "long": {
        "video": FIXTURES_DIR / "benchmark_95min_sourcecopy_faststart.mp4",
        "protocol": FIXTURES_DIR / "protocol.csv",
        "baseline": BASELINES_DIR / "long_default.json",
    },
    "speed-source": {
        "video": INPUT_VIDEOS_DIR / "benchmark_source_speed_check.mp4",
        "protocol": FIXTURES_DIR / "protocol.csv",
        "baseline": BASELINES_DIR / "speed_source_default.json",
    },
}


def _ensure_inputs(case_name: str) -> dict:
    if case_name not in CASES:
        raise SystemExit(f"Unknown case: {case_name}")

    cfg = CASES[case_name]
    missing = [str(path) for path in (cfg["video"], cfg["protocol"], MODEL_PATH) if not Path(path).exists()]
    if missing:
        raise SystemExit(f"Missing benchmark inputs: {', '.join(missing)}")
    return cfg


def _run_case(case_name: str) -> dict:
    cfg = _ensure_inputs(case_name)
    events: list[str] = []
    started = time.perf_counter()
    result = run_protocol_analysis(
        cfg["video"],
        cfg["protocol"],
        MODEL_PATH,
        settings=dict(DEFAULT_SETTINGS),
        partial_cb=None,
        check_cancel=lambda: False,
        progress_cb=lambda _p: None,
        event_cb=lambda msg: events.append(str(msg)),
    )
    runtime_sec = round(time.perf_counter() - started, 3)
    payload = {
        "case": case_name,
        "video": cfg["video"].name,
        "protocol": cfg["protocol"].name,
        "settings": dict(DEFAULT_SETTINGS),
        "runtime_sec": runtime_sec,
        "timestamps": result["timestamps"],
        "results_text": result["results_text"],
        "event_count": len(events),
        "events_tail": events[-10:],
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_path = RUNS_DIR / f"{case_name}_{stamp}.json"
    run_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    payload["run_path"] = str(run_path)
    return payload


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _compare_results(current: dict, baseline: dict, slowdown_limit: float) -> list[str]:
    errors: list[str] = []
    if current["timestamps"] != baseline.get("timestamps"):
        errors.append("timestamps differ from baseline")
    if current["results_text"] != baseline.get("results_text"):
        errors.append("results_text differs from baseline")

    baseline_runtime = float(baseline.get("runtime_sec", 0))
    runtime_limit = round(baseline_runtime * slowdown_limit, 3)
    current["runtime_limit_sec"] = runtime_limit
    if baseline_runtime > 0 and current["runtime_sec"] > runtime_limit:
        errors.append(
            f"runtime {current['runtime_sec']:.3f}s exceeds limit {runtime_limit:.3f}s "
            f"(baseline {baseline_runtime:.3f}s, factor {slowdown_limit:.2f})"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reproducible analysis benchmarks.")
    parser.add_argument("--case", choices=sorted(CASES), default="short")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="write or overwrite the baseline with the current run",
    )
    parser.add_argument(
        "--slowdown-limit",
        type=float,
        default=1.25,
        help="max allowed slowdown versus baseline before failing",
    )
    args = parser.parse_args()

    current = _run_case(args.case)
    baseline_path = CASES[args.case]["baseline"]
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)

    if args.write_baseline or not baseline_path.exists():
        baseline_path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[baseline] saved {baseline_path}")
        print(f"[run] saved {current['run_path']}")
        print(json.dumps(current, indent=2, ensure_ascii=False))
        return 0

    baseline = _load_json(baseline_path)
    errors = _compare_results(current, baseline, args.slowdown_limit)
    print(f"[baseline] {baseline_path}")
    print(f"[run] {current['run_path']}")
    print(json.dumps(current, indent=2, ensure_ascii=False))
    if errors:
        for error in errors:
            print(f"[fail] {error}")
        return 1
    print("[ok] current run matches baseline output and runtime limit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
