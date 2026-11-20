# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The computesdk-bench runner entrypoint: runs burst_TTI or DAX benchmark on
Substrate (or cloud providers) via ComputeSDK, produces results JSON and SVG
charts, writes run.json and computesdk_stats.csv, and uploads all artifacts to
gs://<REPORT_BUCKET>/<REPORT_PREFIX>/<BENCHMARK_RUN_ID>/<BENCHMARK_REPORT_FOLDER>/.
"""

import base64
import csv
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from google.cloud import storage

APP_DIR = Path("/app")
REPORT_DIR = Path("/reports")
DEFAULT_PREFIX = "reports"
UPLOAD_FAILED_EXIT = 3
FAILED_EXIT = 4


class Stopped(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"stopped by signal {signum}")
        self.signum = signum


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()


def upload(bucket_name: str, prefix: str, folder: str) -> bool:
    """Uploads every file in REPORT_DIR to GCS."""
    ok = True
    try:
        bucket = storage.Client().bucket(bucket_name)
    except Exception as exc:
        print(f"Upload failed: cannot reach bucket {bucket_name}: {exc}", flush=True)
        return False
    for path in sorted(p for p in REPORT_DIR.rglob("*") if p.is_file()):
        name = f"{prefix.strip('/')}/{folder}/{path.relative_to(REPORT_DIR).as_posix()}"
        content_type = "application/json"
        if path.suffix == ".svg":
            content_type = "image/svg+xml"
        elif path.suffix == ".csv":
            content_type = "text/csv"
        elif path.suffix in (".txt", ".log"):
            content_type = "text/plain"
        try:
            bucket.blob(name).upload_from_filename(path, content_type=content_type)
            print(f"Uploaded {path.name} to gs://{bucket_name}/{name}", flush=True)
        except Exception as exc:
            ok = False
            print(f"Upload of {path.name} failed: {exc}", flush=True)
    return ok


def write_run_json(started: float, ended: float, status: int, extra_info: dict) -> None:
    try:
        passed = json.loads(base64.b64decode(os.environ.get("BENCHMARK_RUN_INFO", "")))
    except Exception as exc:
        passed = {"run_info_errors": {"BENCHMARK_RUN_INFO": f"cannot decode: {exc}"}}

    info = {
        "run_id": os.environ.get("BENCHMARK_RUN_ID"),
        "test_case": os.environ.get("BENCHMARK_CASE") or os.environ.get("BENCHMARK_RECORD_NAME"),
        "record": os.environ.get("BENCHMARK_RECORD_NAME"),
        "repeat": int(os.environ.get("BENCHMARK_REPEAT") or 0) or None,
        "status": status,
        "started": iso(started),
        "ended": iso(ended),
        **extra_info,
        **passed,
    }
    (REPORT_DIR / "run.json").write_text(json.dumps(info, indent=2) + "\n")


def generate_stats_csv(benchmark_name: str, results_json_path: Path, out_csv_path: Path) -> None:
    """Extracts summary metrics to CSV for ingestion into BigQuery and reporting."""
    if not results_json_path.is_file():
        return
    try:
        data = json.loads(results_json_path.read_text())
        results = data.get("results") or []
        if not results:
            return

        with out_csv_path.open("w", newline="") as f:
            writer = csv.writer(f)
            if benchmark_name in ("burst-tti", "burst_tti"):
                writer.writerow(["provider", "mode", "median_tti_ms", "p95_tti_ms", "p99_tti_ms", "composite_score", "success_rate"])
                for r in results:
                    summary = r.get("summary", {}).get("ttiMs", {})
                    writer.writerow([
                        r.get("provider"),
                        r.get("mode"),
                        summary.get("median"),
                        summary.get("p95"),
                        summary.get("p99"),
                        r.get("compositeScore"),
                        r.get("successRate"),
                    ])
            elif benchmark_name == "dax":
                writer.writerow([
                    "provider", "total_ms_median", "prepare_ms_median", "bun_download_ms_median",
                    "bun_unpack_ms_median", "clone_ms_median", "install_ms_median", "typecheck_ms_median", "success_rate"
                ])
                for r in results:
                    summary = r.get("summary", {})
                    writer.writerow([
                        r.get("provider"),
                        summary.get("totalMs", {}).get("median"),
                        summary.get("prepareMs", {}).get("median"),
                        summary.get("bunDownloadMs", {}).get("median"),
                        summary.get("bunUnpackMs", {}).get("median"),
                        summary.get("cloneMs", {}).get("median"),
                        summary.get("installMs", {}).get("median"),
                        summary.get("typecheckMs", {}).get("median"),
                        r.get("successRate"),
                    ])
    except Exception as exc:
        print(f"Failed to generate stats CSV: {exc}", file=sys.stderr)


def main() -> int:
    def handle_signal(signum: int, _) -> None:
        raise Stopped(signum)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(APP_DIR)

    benchmark_name = os.environ.get("BENCHMARK_NAME", "burst-tti").lower().replace("_", "-")
    provider = os.environ.get("BENCHMARK_PROVIDER", "substrate")
    iterations = os.environ.get("BENCHMARK_ITERATIONS", "5" if benchmark_name == "burst-tti" else "1")
    concurrency = os.environ.get("BENCHMARK_CONCURRENCY", "3" if benchmark_name == "burst-tti" else "1")

    # Target benchmark file
    if benchmark_name in ("burst-tti", "tti"):
        bench_file = "benchmarks/sandbox/tti.bench.ts"
        svg_cmd = ["pnpm", "run", "generate-svg:burst"]
        result_src = APP_DIR / "results" / "burst_tti" / "latest.json"
        svg_src = APP_DIR / "burst_tti.svg"
    elif benchmark_name == "dax":
        bench_file = "benchmarks/sandbox/dax.bench.ts"
        svg_cmd = ["pnpm", "run", "generate-dax-svg"]
        result_src = APP_DIR / "results" / "sandbox-dax" / "latest.json"
        svg_src = APP_DIR / "dax.svg"
    else:
        print(f"Unknown benchmark name: {benchmark_name}", file=sys.stderr)
        return FAILED_EXIT

    env = {
        **os.environ,
        "BENCHSDK_NO_INGEST": "1",
        "NODE_OPTIONS": "--no-warnings",
    }

    cmd = [
        "pnpm", "exec", "tsx", "packages/benchsdk-runner/dist/bin.js", "run",
        bench_file,
        "--iterations", str(iterations),
        "--concurrency", str(concurrency),
        "--provider", provider,
        "--dry-run",
    ]

    print(f"Running ComputeSDK benchmark: {' '.join(cmd)}", flush=True)
    started = time.time()
    exit_status = 0
    extra_info = {
        "benchmark_name": benchmark_name,
        "provider": provider,
        "iterations": iterations,
        "concurrency": concurrency,
    }

    log_path = REPORT_DIR / "runner.log"
    with log_path.open("w") as log_file:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log_file.write(line)
            proc.wait()
            exit_status = proc.returncode
        except Stopped as stop:
            print(f"Interrupted by signal {stop.signum}", flush=True)
            exit_status = 128 + stop.signum
        except Exception as exc:
            print(f"Benchmark execution failed: {exc}", file=sys.stderr)
            exit_status = FAILED_EXIT

    ended = time.time()

    # Re-generate visual chart
    if exit_status == 0:
        try:
            print(f"Generating SVG chart: {' '.join(svg_cmd)}", flush=True)
            subprocess.run(svg_cmd, check=True, env=env)
        except Exception as exc:
            print(f"Warning: SVG generation failed: {exc}", file=sys.stderr)

    # Collect artifacts to /reports
    if result_src.is_file():
        shutil.copy2(result_src, REPORT_DIR / "results.json")
        generate_stats_csv(benchmark_name, REPORT_DIR / "results.json", REPORT_DIR / "computesdk_stats.csv")

    if svg_src.is_file():
        shutil.copy2(svg_src, REPORT_DIR / svg_src.name)

    write_run_json(started, ended, exit_status, extra_info)

    # Upload to GCS if configured
    bucket = os.environ.get("REPORT_BUCKET")
    if bucket:
        prefix = os.environ.get("REPORT_PREFIX", DEFAULT_PREFIX)
        folder = os.environ.get("BENCHMARK_REPORT_FOLDER", "default")
        run_id = os.environ.get("BENCHMARK_RUN_ID", "")
        dest_folder = f"{run_id}/{folder}" if run_id else folder
        if not upload(bucket, prefix, dest_folder):
            return UPLOAD_FAILED_EXIT

    return exit_status


if __name__ == "__main__":
    sys.exit(main())
