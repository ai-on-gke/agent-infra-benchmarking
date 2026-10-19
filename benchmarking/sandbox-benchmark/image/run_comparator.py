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

"""The comparator image's entrypoint: runs ai-sandbox-benchmark's tests on one
of its providers, writes each run of each test to results.json and how the
whole went to run.json, the same as BigQuery reads them in RUNS_JSONL and
RUN_JSONL, and uploads them to
gs://<REPORT_BUCKET>/<REPORT_PREFIX>/<BENCHMARK_RUN_ID>/<BENCHMARK_RECORD_NAME>/<provider>/.

It drives comparator.py's SandboxExecutor rather than its command line, which
numbers the tests in directory order and whose history.json leaves out the
errors a provider returns rather than raises: every error of the provider's.

Read from the environment, besides the provider's own settings, such as
substrate's SUBSTRATE_*, e2b's E2B_API_KEY, daytona's DAYTONA_*, modal's
MODAL_TOKEN_* and anthropic's ANTHROPIC_API_KEY:

    BENCHMARK_PROVIDER     the provider, substrate by default
    BENCHMARK_TESTS        the tests' function names, comma-separated
    BENCHMARK_RUNS         how many times each test runs, once for a single-run one
    BENCHMARK_WARMUP_RUNS  the runs of each before those, not recorded; 0 by default
    MAX_FAILURE_RATIO      the share of runs, 0 to 1, that may fail
    BENCHMARK_RUN_ID, BENCHMARK_RECORD_NAME, BENCHMARK_CASE, BENCHMARK_REPEAT
                           what the run is recorded as
    BENCHMARK_RUN_INFO     base64 JSON the scenario-runner adds to run.json
    REPORT_BUCKET          empty leaves the files on the pod
    REPORT_PREFIX          reports when unset or empty

Exits 0; FAILED_EXIT for a run more than MAX_FAILURE_RATIO of whose runs
failed, or none ran; 128 plus the signal for one stopped; UPLOAD_FAILED_EXIT
for one that passed but did not all upload.
"""

import asyncio
import base64
import datetime
import json
import os
import signal
import statistics
import sys
import time
from pathlib import Path

from google.cloud import storage

APP_DIR = Path(__file__).resolve().parent
REPORT_DIR = Path("/reports")
PROVIDER = os.environ.get("BENCHMARK_PROVIDER") or "substrate"
DEFAULT_PREFIX = "reports"
# A test's output is kept to its last OUTPUT_TAIL characters.
OUTPUT_TAIL = 2000
UPLOAD_FAILED_EXIT = 3
# Newline-delimited JSON, as BigQuery's external tables in
# automation-infra/terraform/modules/reports/main.tf read it.
RUNS_JSONL = "sandbox_benchmark_runs.jsonl"
RUN_JSONL = "sandbox_benchmark_run.jsonl"
# What of run.json goes into RUN_JSONL.
RUN_JSONL_KEYS = (
    "status", "started", "ended", "tests", "runs", "warmup_runs", "max_failure_ratio", "commit", "substrate_commit",
    "env_commit", "ai_sandbox_benchmark_commit",
)  # fmt: skip
FAILED_EXIT = 4


class Stopped(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"stopped by signal {signum}")
        self.signum = signum


def load_comparator():
    # It finds tests/ and config.yml from the working directory.
    os.chdir(APP_DIR)
    sys.path.insert(0, str(APP_DIR))
    # SandboxExecutor checks the API keys of the providers in sys.argv's
    # --providers, and of every provider when it names none.
    sys.argv = [sys.argv[0], "--providers", PROVIDER]
    import comparator

    return comparator


def select_tests(comparator, names: list[str]) -> dict:
    """comparator's tests by its ids, those of names."""
    by_name = {func.__name__: (test_id, func) for test_id, func in comparator.defined_tests.items()}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise SystemExit(f"no test {', '.join(unknown)}; there are {', '.join(sorted(by_name))}")
    return dict(by_name[name] for name in names)


def records(tests: dict, results: dict) -> list[dict]:
    """Each run of each test: its metrics in milliseconds, and its errors, of
    which a failed run has at least one."""
    out = []
    for test_id, func in tests.items():
        test_runs = results.get(f"test_{test_id}", {})
        for number in sorted(int(key.removeprefix("run_")) for key in test_runs):
            result = test_runs[f"run_{number}"].get(PROVIDER) or {}
            metrics = result.get("metrics")
            errors = list(getattr(metrics, "errors", []))
            if result.get("error") and result["error"] not in errors:
                errors.append(result["error"])
            output = result.get("output")
            output = str(output) if output is not None else None
            if output and output.startswith("Error:") and not errors:
                errors.append(output)
            if not result:
                errors.append(f"no result from {PROVIDER}")
            out.append(
                {
                    "test": func.__name__,
                    "run": number,
                    "metrics": {
                        name: [float(v) for v in values] for name, values in getattr(metrics, "metrics", {}).items()
                    },
                    "errors": errors,
                    "output": output[-OUTPUT_TAIL:] if output else None,
                }
            )
    return out


def print_summary(runs: list[dict]) -> None:
    for test in dict.fromkeys(r["test"] for r in runs):
        of_test = [r for r in runs if r["test"] == test]
        passed = [r for r in of_test if not r["errors"]]
        medians = {}
        for run in passed:
            for name, values in run["metrics"].items():
                medians.setdefault(name, []).extend(values)
        # A metric a run added no measurement to has no values.
        stats = ", ".join(
            f"{name} {statistics.median(values):.3f}ms" for name, values in medians.items() if values
        )
        print(
            f"{test}: {len(passed)} of {len(of_test)} runs passed" + (f"; medians: {stats}" if stats else ""),
            flush=True,
        )
        for run in of_test:
            for error in run["errors"]:
                print(f"  run {run['run']}: {error}", flush=True)


def count_failures(runs: list[dict]) -> dict:
    failed = sum(1 for run in runs if run["errors"])
    return {"runs": len(runs), "failed": failed, "ratio": failed / len(runs) if runs else None}


def within_limits(failures: dict, max_ratio: float) -> bool:
    if not failures["runs"]:
        print("No test ran.", flush=True)
        return False
    print(
        f"Failed runs: {failures['failed']} of {failures['runs']} ({failures['ratio']:.3%}); "
        f"max-failure-ratio allows {max_ratio:.3%}.",
        flush=True,
    )
    return failures["ratio"] <= max_ratio


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()


def write_run_json(started: float, ended: float, status: int, failures: dict | None, settings: dict) -> None:
    """How the run went, followed by what the scenario-runner passed in
    BENCHMARK_RUN_INFO."""
    try:
        passed = json.loads(base64.b64decode(os.environ.get("BENCHMARK_RUN_INFO", "")))
    except ValueError as exc:
        passed = {"run_info_errors": {"BENCHMARK_RUN_INFO": f"cannot decode it: {exc}"}}
    info = {
        "run_id": os.environ.get("BENCHMARK_RUN_ID"),
        "test_case": os.environ.get("BENCHMARK_CASE") or os.environ.get("BENCHMARK_RECORD_NAME"),
        "record": os.environ.get("BENCHMARK_RECORD_NAME"),
        "repeat": int(os.environ.get("BENCHMARK_REPEAT") or 0) or None,
        "status": status,
        "started": iso(started),
        "ended": iso(ended),
        "max_failure_ratio": float(os.environ.get("MAX_FAILURE_RATIO", "0")),
        "failures": failures,
        **settings,
        **passed,
    }
    (REPORT_DIR / "run.json").write_text(json.dumps(info, indent=2) + "\n")
    case = info.get("case") or {}
    row = {
        "provider": PROVIDER,
        **{key: info.get(key) for key in RUN_JSONL_KEYS},
        "total_runs": (failures or {}).get("runs"),
        "failed_runs": (failures or {}).get("failed"),
        "sandbox_class": case.get("sandbox-class") if PROVIDER == "substrate" else None,
        "actor_memory": case.get("actor-memory") if PROVIDER == "substrate" else None,
        "comparator_image": next(iter((info.get("images") or {}).values()), None),
    }
    (REPORT_DIR / RUN_JSONL).write_text(json.dumps(row) + "\n")


def write_runs_jsonl(runs: list[dict]) -> None:
    """Each run of each test as a line; its metrics as a list, as their names
    are no column names."""
    lines = [
        {
            "provider": PROVIDER,
            "test": run["test"],
            "run": run["run"],
            "failed": bool(run["errors"]),
            "errors": run["errors"],
            "metrics": [{"name": name, "value_ms": v} for name, values in run["metrics"].items() for v in values],
        }
        for run in runs
    ]
    (REPORT_DIR / RUNS_JSONL).write_text("".join(json.dumps(line) + "\n" for line in lines))


def upload(bucket_name: str, prefix: str, folder: str) -> bool:
    """Uploads every file in REPORT_DIR; whether they all went. The runner's
    account has objectCreator and nothing more, so one already there fails."""
    ok = True
    try:
        bucket = storage.Client().bucket(bucket_name)
    except Exception as exc:
        print(f"Upload failed: cannot reach bucket {bucket_name}: {exc}", flush=True)
        return False
    for path in sorted(p for p in REPORT_DIR.rglob("*") if p.is_file()):
        name = f"{prefix.strip('/')}/{folder}/{path.relative_to(REPORT_DIR).as_posix()}"
        try:
            bucket.blob(name).upload_from_filename(path, content_type="application/json")
        except Exception as exc:
            ok = False
            print(f"Upload of {path.name} failed: {exc}", flush=True)
        else:
            print(f"Uploaded gs://{bucket_name}/{name}", flush=True)
    return ok


def main() -> int:
    bucket = os.environ.get("REPORT_BUCKET", "")
    prefix = os.environ.get("REPORT_PREFIX") or DEFAULT_PREFIX
    # Read before the run, so a missing one fails it before it starts.
    folder = f"{os.environ['BENCHMARK_RUN_ID']}/{os.environ['BENCHMARK_RECORD_NAME']}/{PROVIDER}" if bucket else ""
    max_ratio = float(os.environ.get("MAX_FAILURE_RATIO", "0"))
    names = [name.strip() for name in os.environ["BENCHMARK_TESTS"].split(",") if name.strip()]
    runs = int(os.environ["BENCHMARK_RUNS"])
    warmup_runs = int(os.environ.get("BENCHMARK_WARMUP_RUNS") or 0)
    settings = {"tests": names, "runs": runs, "warmup_runs": warmup_runs}

    # As PID 1 this gets the pod's SIGTERM, which it ignores without a handler.
    # Raised in the event loop, it cancels the runs, whose providers delete
    # their environments on the way out.
    def stop(signum: int, _frame: object) -> None:
        raise Stopped(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    comparator = load_comparator()
    tests = select_tests(comparator, names)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    status, failures = 0, None
    try:
        executor = comparator.SandboxExecutor(
            warmup_runs=warmup_runs, measurement_runs=runs, num_concurrent_providers=1
        )
        results = asyncio.run(executor.run_comparison(tests, [PROVIDER], runs, ""))
    except Stopped as exc:
        print(f"Stopped by signal {exc.signum}; nothing is recorded.", flush=True)
        status = 128 + exc.signum
    ended = time.time()

    if not status:
        recorded = records(tests, results)
        (REPORT_DIR / "results.json").write_text(json.dumps({"provider": PROVIDER, "runs": recorded}, indent=2) + "\n")
        write_runs_jsonl(recorded)
        print_summary(recorded)
        failures = count_failures(recorded)
        if not within_limits(failures, max_ratio):
            status = FAILED_EXIT
    write_run_json(started, ended, status, failures, settings)

    if not bucket:
        print(f"REPORT_BUCKET is empty; the reports stay in {REPORT_DIR} on the pod.", flush=True)
        return status
    uploaded = upload(bucket, prefix, folder)
    return status or (0 if uploaded else UPLOAD_FAILED_EXIT)


if __name__ == "__main__":
    sys.exit(main())
