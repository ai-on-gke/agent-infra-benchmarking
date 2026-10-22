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

"""Where a run's reports go, whatever its case's type: under
<report prefix>/<run id>/<record>/ in a bucket, and copied into
REPORTS_DIR/<run id>/<record>/ on a local run; beside them the logs of the
run's Jobs and cleanup.json, what the run left in its atespace."""

from __future__ import annotations

import json
import re
import subprocess
import time

from google.cloud import storage

from .. import RUNNER_DIR, workloads
from ..infra.cluster import Cluster
from ..log import ON_CLOUD_RUN, log
from ..process import output

# Where a local run copies the uploads to, as <run-id>/<case>/ beneath it, as
# they are under the report prefix in the bucket.
REPORTS_DIR = RUNNER_DIR.parent.parent / "reports"
# The default report prefix, which a case's own bucket deletes reports under
# after its report_retention_days.
REPORT_PREFIX = "reports"
# The run id and case name end up as label values.
LABEL_RE = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")
# How long after the run its actors get to be gone before the ones left are
# recorded in cleanup.json.
ACTOR_DELETE_WAIT_SECS = 60


class ReportError(RuntimeError):
    pass


def refuse_reused(bucket: str, report_prefix: str, run_id: str, name: str) -> None:
    """Refuses a run id the case was already recorded under, whose reports
    the bucket or the local copy hold: they would be mixed with this run's."""
    prefix = f"{report_prefix}/{run_id}/{name}/"
    local = REPORTS_DIR / run_id / name
    if local.is_dir() and any(local.iterdir()):
        raise ReportError(f"{local} already holds reports of {name} under run {run_id}; pick another run id")
    try:
        found = next(iter(storage.Client().list_blobs(bucket, prefix=prefix, max_results=1)), None)
    except Exception as exc:
        raise ReportError(f"cannot check gs://{bucket}/{prefix} for an earlier run: {exc}") from None
    if found is not None:
        raise ReportError(f"gs://{bucket}/{prefix} already holds reports; pick another run id")


def record_cleanup(cluster: Cluster, atespace: str, bucket: str, report_prefix: str, run_id: str, name: str) -> None:
    """Deletes the actors a run left in its atespace and writes what it found
    as cleanup.json beside the reports, in the bucket and locally. A run
    deletes its own actors as it ends; one it lost track of, or one stuck
    DELETING (a substrate fault no delete clears), would otherwise go unrecorded
    and, on a dev infra, hold a worker the next run cannot use."""
    record = {
        "atespace": atespace,
        "listed": False,
        "deleted": 0,
        # Actor name -> the error its delete was refused with.
        "delete_failed": {},
        "wait_secs": ACTOR_DELETE_WAIT_SECS,
        "left": [],
    }
    try:
        with workloads.kubectl_ate(cluster) as ate:

            def actors() -> list[dict]:
                listed = json.loads(output(ate("get", "actors", "-a", atespace, "-o", "json")))
                return [
                    {"name": a["metadata"]["name"], "state": (a.get("status") or {}).get("state", "ACTOR_STATE_UNKNOWN")}
                    for a in listed.get("actors") or []
                ]

            # Kept current, so a list that fails later leaves the last one seen.
            left = record["left"] = actors()
            record["listed"] = True
            for actor in left:
                proc = subprocess.run(ate("delete", "actor", actor["name"], "-a", atespace, "--any-state"),
                                      capture_output=True, text=True)  # fmt: skip
                if proc.returncode:
                    record["delete_failed"][actor["name"]] = proc.stderr.strip()
            record["deleted"] = len(left) - len(record["delete_failed"])
            deadline = time.monotonic() + ACTOR_DELETE_WAIT_SECS
            while left and time.monotonic() < deadline:
                time.sleep(5)
                left = record["left"] = actors()
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        log(f"reports: could not list the actors left in {atespace}: {exc}", "WARNING")
    for actor, error in record["delete_failed"].items():
        log(f"reports: could not delete actor {actor} in {atespace}: {error}", "WARNING")
    if record["left"]:
        names = ", ".join(f"{a['name']} ({a['state']})" for a in record["left"])
        log(f"reports: {len(record['left'])} actor(s) still in {atespace} {ACTOR_DELETE_WAIT_SECS}s after"
            f" deleting them: {names}", "WARNING")  # fmt: skip
    elif record["listed"]:
        log(f"reports: no actors left in {atespace} ({record['deleted']} deleted)")
    body = json.dumps(record, indent=2) + "\n"
    try:
        storage.Client().bucket(bucket).blob(f"{report_prefix}/{run_id}/{name}/cleanup.json").upload_from_string(body)
    except Exception as exc:
        log(f"reports: could not upload cleanup.json: {exc}", "WARNING")
    if not ON_CLOUD_RUN:
        local = REPORTS_DIR / run_id / name
        local.mkdir(parents=True, exist_ok=True)
        (local / "cleanup.json").write_text(body)


def upload_logs(
    cluster: Cluster, namespace: str, jobs: tuple[str, ...], bucket: str, report_prefix: str, run_id: str, name: str
) -> None:
    """Puts each Job's log, as <job>.log, beside the reports in bucket. Taken
    from the pods rather than uploaded by them so a crash is in it too.
    Best-effort: a log not uploaded is still on its pod until the Job's TTL."""
    prefix = f"{report_prefix}/{run_id}/{name}"
    for job in jobs:
        # Every pod's, each line prefixed with its pod, as a Job can have several.
        cmd = cluster.kubectl("logs", f"--selector=job-name={job}", "--prefix", "--tail=-1", "--max-log-requests=50",
                              namespace=namespace)  # fmt: skip
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and not proc.stdout:
            proc.returncode, proc.stderr = 1, "no pods"
        if proc.returncode != 0:
            log(f"reports: no log of {job} to upload: {proc.stderr.strip()}", "WARNING")
            continue
        uri = f"gs://{bucket}/{prefix}/{job}.log"
        try:
            storage.Client().bucket(bucket).blob(f"{prefix}/{job}.log").upload_from_string(
                proc.stdout, content_type="text/plain"
            )
        except Exception as exc:
            log(f"reports: could not upload {uri}: {exc}", "WARNING")
        else:
            log(f"reports: uploaded {uri}")


def download_reports(bucket: str, report_prefix: str, run_id: str, name: str) -> bool:
    """Copies what the run uploaded into REPORTS_DIR/<run_id>/<name>/, while
    the bucket, if it is the case's own, is still there, and returns whether
    there was anything to copy and all of it was."""
    prefix = f"{report_prefix}/{run_id}/{name}/"
    dest = REPORTS_DIR / run_id / name
    log(f"reports: downloading the reports in gs://{bucket}/{prefix} to {dest}")
    try:
        blobs = list(storage.Client().list_blobs(bucket, prefix=prefix))
        for blob in blobs:
            path = dest / blob.name.removeprefix(prefix)
            path.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(path)
            log(f"reports: downloaded {path}")
    except Exception as exc:
        log(f"reports: could not download gs://{bucket}/{prefix}: {exc}", "WARNING")
        return False
    if blobs:
        log(f"reports: downloaded {len(blobs)} report files to {dest}")
    else:
        log(f"reports: no reports in gs://{bucket}/{prefix} to download", "WARNING")
    return bool(blobs)
