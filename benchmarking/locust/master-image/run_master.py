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
writes custom_history's and cluster_vitals' CSVs beside locust's, then
uploads every file in LOCUST_CSV's folder to
gs://<REPORT_BUCKET>/<REPORT_PREFIX>/<BENCHMARK_RUN_ID>/<BENCHMARK_TEST_CASE>/,
REPORT_PREFIX being reports when unset or empty.

An empty REPORT_BUCKET leaves the files on the pod. Exits with locust's
status, or UPLOAD_FAILED_EXIT for a run that passed but did not all upload.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import cluster_vitals
import custom_history
import upload

UPLOAD_FAILED_EXIT = 3


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


def main() -> int:
    csv_prefix = Path(os.environ["LOCUST_CSV"])
    report_dir = csv_prefix.parent
    bucket = os.environ.get("REPORT_BUCKET", "")
    prefix = os.environ.get("REPORT_PREFIX") or upload.DEFAULT_PREFIX
    # Read before the run, so a missing one fails it before it starts.
    folder = f"{os.environ['BENCHMARK_RUN_ID']}/{os.environ['BENCHMARK_TEST_CASE']}" if bucket else ""

    # locust does not create the folder its CSVs go in.
    report_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    locust = subprocess.Popen([sys.executable, "-m", "locust", *sys.argv[1:]])

    # As PID 1 this gets the pod's SIGTERM, which locust needs to stop its
    # worker and write its CSVs.
    def forward(signum: int, _frame: object) -> None:
        locust.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    status = locust.wait()
    if status < 0:
        status = 128 - status
    print(f"locust exited with {status}", flush=True)

    write_reports(csv_prefix, started)

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
