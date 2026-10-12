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

"""The master image's entrypoint: runs locust with this process's arguments,
writes custom_history's and cluster_vitals' CSVs beside locust's, and run.json
and README.md, then uploads every file in LOCUST_CSV's folder to
gs://<REPORT_BUCKET>/<REPORT_PREFIX>/<BENCHMARK_RUN_ID>/<BENCHMARK_TEST_CASE>/,
REPORT_PREFIX being reports when unset or empty.

An empty REPORT_BUCKET leaves the files on the pod. Exits with locust's
status; TOO_MANY_FAILURES_EXIT for a run whose failed requests are more than
MAX_FAILURE_RATIO of them, or that made none; UPLOAD_FAILED_EXIT for a run
that passed but did not all upload.
"""

import base64
import csv
import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import cluster_vitals
import custom_history
import readme
import upload

UPLOAD_FAILED_EXIT = 3
TOO_MANY_FAILURES_EXIT = 4


def write_reports(csv_prefix: Path, started: float) -> None:
    """Writes the reports Prometheus holds the data of, once the run's last
    scrape is in. Each is a by-product of the run rather than its verdict, so
    one not written fails nothing, the others included."""
    time.sleep(custom_history.SETTLE_SECS)
    ended = time.time()
    run_id, test_case = os.environ.get("BENCHMARK_RUN_ID", ""), os.environ.get("BENCHMARK_TEST_CASE", "")
    reports = {
        custom_history.FILENAME: lambda: custom_history.write(csv_prefix, run_id, test_case, started, ended),
        cluster_vitals.FILENAME: lambda: cluster_vitals.write(csv_prefix, started, ended),
    }
    for filename, write in reports.items():
        try:
            print(f"Wrote {write()}", flush=True)
        except (OSError, KeyError, ValueError) as exc:
            print(f"No {filename}: {exc}", flush=True)


def count_failures(csv_prefix: Path) -> dict | None:
    """The run's requests, failed requests and load generator exceptions, from
    the CSVs locust wrote on exit; None, and said why, if they cannot be read."""
    try:
        with open(f"{csv_prefix}_stats.csv", newline="") as f:
            total = next(row for row in csv.DictReader(f) if row["Name"] == "Aggregated")
        requests, failed = int(total["Request Count"]), int(total["Failure Count"])
        with open(f"{csv_prefix}_exceptions.csv", newline="") as f:
            raised = sum(int(row["Count"]) for row in csv.DictReader(f))
    except (OSError, StopIteration, KeyError, ValueError) as exc:
        print(f"Cannot count the run's failures: {type(exc).__name__}: {exc}", flush=True)
        return None
    return {"requests": requests, "failed": failed, "ratio": failed / requests if requests else None, "exceptions": raised}


def within_limits(failures: dict | None, max_ratio: float) -> bool:
    """Whether the run stayed within its max-failure-ratio and raised no load
    generator exceptions. --exit-code-on-error 0 keeps both out of locust's own
    exit status, so this is what fails a run over them."""
    if failures is None:
        return False
    if not failures["requests"]:
        print("The run made no requests.", flush=True)
        return False
    print(
        f"Failed requests: {failures['failed']} of {failures['requests']} ({failures['ratio']:.3%}); "
        f"max-failure-ratio allows {max_ratio:.3%}.",
        flush=True,
    )
    if failures["exceptions"]:
        print(f"The load generator raised {failures['exceptions']} exception(s).", flush=True)
    return failures["ratio"] <= max_ratio and not failures["exceptions"]


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()


def write_run_json(csv_prefix: Path, started: float, ended: float, status: int, failures: dict | None) -> None:
    """Writes run.json and its README.md beside the reports: how the run went,
    followed by what the scenario-runner passed in BENCHMARK_RUN_INFO. The
    charts read the steady-state window and the repeat from it."""
    try:
        passed = json.loads(base64.b64decode(os.environ.get("BENCHMARK_RUN_INFO", "")))
    except ValueError as exc:
        passed = {"run_info_errors": {"BENCHMARK_RUN_INFO": f"cannot decode it: {exc}"}}
    try:
        steady = json.loads(Path(f"{csv_prefix}_steady_window.json").read_text())
    except (OSError, ValueError):
        steady = None
    info = {
        "run_id": os.environ.get("BENCHMARK_RUN_ID"),
        "test_case": os.environ.get("BENCHMARK_CASE") or os.environ.get("BENCHMARK_TEST_CASE"),
        "record": os.environ.get("BENCHMARK_TEST_CASE"),
        "repeat": int(os.environ.get("BENCHMARK_REPEAT") or 0) or None,
        "status": status,
        "started": iso(started),
        "ended": iso(ended),
        "max_failure_ratio": float(os.environ.get("MAX_FAILURE_RATIO", "0")),
        "failures": failures,
        "warm_up": f"{steady['warm_up_secs']}s" if steady else None,
        "steady_window": {"start": steady["start"], "end": steady["end"]} if steady else None,
        "master_args": sys.argv[1:],
        **passed,
    }
    report_dir = csv_prefix.parent
    (report_dir / "run.json").write_text(json.dumps(info, indent=2) + "\n")
    try:
        (report_dir / "README.md").write_text(readme.render(info))
    except Exception as exc:
        # run.json is the record either way.
        print(f"README.md skipped: {type(exc).__name__}: {exc}", flush=True)


def main() -> int:
    csv_prefix = Path(os.environ["LOCUST_CSV"])
    report_dir = csv_prefix.parent
    bucket = os.environ.get("REPORT_BUCKET", "")
    prefix = os.environ.get("REPORT_PREFIX") or upload.DEFAULT_PREFIX
    # Read before the run, so a missing one fails it before it starts.
    folder = f"{os.environ['BENCHMARK_RUN_ID']}/{os.environ['BENCHMARK_TEST_CASE']}" if bucket else ""
    max_ratio = float(os.environ.get("MAX_FAILURE_RATIO", "0"))

    # As PID 1 this gets the pod's SIGTERM, which locust needs to stop its
    # worker and write its CSVs. Installed before locust starts, as PID 1
    # ignores a signal it has no handler for; one that lands before locust
    # exists is passed on once it does.
    locust: subprocess.Popen | None = None
    received: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        received.append(signum)
        if locust is not None:
            locust.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    # locust does not create the folder its CSVs go in.
    report_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    locust = subprocess.Popen([sys.executable, "-m", "locust", *sys.argv[1:]])
    if received:
        locust.send_signal(received[0])
    status = locust.wait()
    if status < 0:
        status = 128 - status
    print(f"locust exited with {status}", flush=True)
    ended = time.time()

    write_reports(csv_prefix, started)
    failures = count_failures(csv_prefix)
    # A stopped run did not run its course, so its count is not a verdict.
    if status == 0 and received:
        status = 128 + received[0]
    elif status == 0 and not within_limits(failures, max_ratio):
        status = TOO_MANY_FAILURES_EXIT
    write_run_json(csv_prefix, started, ended, status, failures)

    if not bucket:
        print(f"REPORT_BUCKET is empty; the reports stay in {report_dir} on the pod.", flush=True)
        return status

    paths = sorted(p for p in report_dir.rglob("*") if p.is_file())
    if not paths:
        print(f"locust wrote nothing to {report_dir} to upload.", flush=True)
        return status
    try:
        results = upload.upload(paths, bucket, folder, report_dir, prefix)
    except upload.UploadError as exc:
        print(f"Upload failed: {exc}", flush=True)
        return status or UPLOAD_FAILED_EXIT
    failed = False
    for path, uri, error in results:
        if error:
            failed = True
            print(f"Upload of {path.name} failed: {error}", flush=True)
        else:
            print(f"Uploaded {uri}", flush=True)
    return status or (UPLOAD_FAILED_EXIT if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
