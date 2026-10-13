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

"""Stub E2BLifecycleUser declaration.

The real load implementation lives in the e2b-worker (boomer-Go) at
cmd/benchmarking/e2b-worker/ and internal/benchmarking/e2b/e2bworkload/; this
Python class is declared only so the master's --class-picker enumerates the
name, mirroring the runworkload.py stub next to it. A Python worker container
would set LOCUST_NO_E2BLIFECYCLE_USER=1 to skip loading this file, leaving the
e2b-worker as the sole owner of E2BLifecycleUser load.

The name is load-bearing twice over, as with runworkload: the master loads
/app/tests/${BENCHMARK_USER_CLASS}.py, and the e2b-worker's boomer.Task.Name is
E2BLifecycleUser (see e2bworkload.E2BUserClass) — the two must match for the
master to attribute the worker's rows to this class.
"""

import os

if os.environ.get("LOCUST_NO_E2BLIFECYCLE_USER") != "1":
    from locust import User, task

    class E2BLifecycleUser(User):
        host = "api.e2b.app"

        @task
        def noop(self) -> None:
            # Unreached under normal operation: the load runs in the e2b-worker
            # container, not here. Body is required because locust validates
            # that every User has at least one @task method.
            pass
