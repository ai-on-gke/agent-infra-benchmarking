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
then uploads every file in LOCUST_CSV's folder to
gs://<REPORT_BUCKET>/reports/<BENCHMARK_RUN_ID>/<BENCHMARK_TEST_CASE>/.

An empty REPORT_BUCKET leaves the files on the pod. Exits with locust's
status, or UPLOAD_FAILED_EXIT for a run that passed but did not all upload.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

import upload

UPLOAD_FAILED_EXIT = 3


def main() -> int:
    report_dir = Path(os.environ["LOCUST_CSV"]).parent
    bucket = os.environ.get("REPORT_BUCKET", "")
    # Read before the run, so a missing one fails it before it starts.
    folder = f"{os.environ['BENCHMARK_RUN_ID']}/{os.environ['BENCHMARK_TEST_CASE']}" if bucket else ""

    # locust does not create the folder its CSVs go in.
    report_dir.mkdir(parents=True, exist_ok=True)
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

    if not bucket:
        print(f"REPORT_BUCKET is empty; the reports stay in {report_dir} on the pod.", flush=True)
        return status

    paths = sorted(p for p in report_dir.rglob("*") if p.is_file())
    if not paths:
        print(f"locust wrote nothing to {report_dir} to upload.", flush=True)
        return status
    try:
        results = upload.upload(paths, bucket, folder, report_dir)
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
