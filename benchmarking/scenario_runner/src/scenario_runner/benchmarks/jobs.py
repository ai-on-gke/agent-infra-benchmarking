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

"""The Kubernetes Jobs a case's run is made of, whatever its type: how one
stands, its log as it comes, and stopping it."""

from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

from ..infra.cluster import Cluster
from ..log import log


class StatusUnread(RuntimeError):
    """kubectl could not read a Job."""


def job_status(cluster: Cluster, job: str, namespace: str) -> str | None:
    """"Complete" or "Failed" once the Job has ended, as its conditions say,
    else None. A Job of several pods ends once all of them have succeeded, or
    one has failed. Raises StatusUnread if the Job cannot be read."""
    proc = subprocess.run(
        cluster.kubectl("get", "job", job, '-o=jsonpath={.status.conditions[?(@.status=="True")].type}',
                        namespace=namespace),
        capture_output=True,
        text=True,
    )  # fmt: skip
    if proc.returncode:
        raise StatusUnread(f"kubectl get job {job}: {proc.stderr.strip()}")
    ended = set(proc.stdout.split()) & {"Complete", "Failed"}
    return ended.pop() if ended else None


def logs_hint(cluster: Cluster, job: str, namespace: str) -> str:
    return f"kubectl logs --selector=job-name={job} --prefix --tail=-1 -n {namespace} --kubeconfig={cluster.kubeconfig}"


@contextmanager
def follow_logs(cluster: Cluster, job: str, namespace: str) -> Iterator[Callable[[int], None]]:
    """Logs the Job's output as it comes, and yields what stops it, after
    giving it up to that many seconds to print its last lines and end."""
    follow = cluster.kubectl("logs", "--follow", f"job/{job}", namespace=namespace)
    proc = subprocess.Popen(follow, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    reader = threading.Thread(target=lambda: [log(line.rstrip("\n")) for line in proc.stdout], daemon=True)
    reader.start()

    def stop(grace: int = 0) -> None:
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
        reader.join()

    try:
        yield stop
    finally:
        stop()


def stop(cluster: Cluster, jobs: tuple[str, ...], namespace: str, timeout: int) -> bool:
    """Deletes the Jobs one after another, each waited for up to timeout
    seconds, and returns whether all are gone. A deleted Job's pod gets
    SIGTERM and its terminationGracePeriodSeconds to wind down."""
    stopped = True
    for job in jobs:
        delete = ["delete", "job", job, "--ignore-not-found", "--cascade=foreground", "--wait", f"--timeout={timeout}s"]
        proc = subprocess.run(cluster.kubectl(*delete, namespace=namespace), capture_output=True, text=True)
        if proc.returncode:
            stopped = False
            log(f"jobs: delete job {job} failed: {proc.stderr.strip()}", "WARNING")
    return stopped
