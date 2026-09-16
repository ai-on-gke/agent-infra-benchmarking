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

"""Stub RunWorkloadUser declaration.

The real load implementation lives in the boomer-Go worker at
internal/benchmarking/boomer/runworkload/; this Python class is declared only
so the master's --class-picker enumerates the name. A Python worker container
would set LOCUST_NO_RUNWORKLOAD_USER=1 to skip loading this file, leaving
boomer as the sole owner of RunWorkloadUser load.

This file is the only locust asset this repo owns — the rest of the image's
Python comes from substrate. It exists because runworkload has no upstream
counterpart, and its name is load-bearing twice over: the master loads
/app/tests/${BENCHMARK_USER_CLASS}.py, and the boomer container is passed
--user-class=${BENCHMARK_USER_CLASS}, which selects the "runworkload" entry
the Go package registers with userclass.Add.
"""

import os

if os.environ.get("LOCUST_NO_RUNWORKLOAD_USER") != "1":
    from locust import User, task
    from common.boomer_config import init_boomer_config

    # Master serves /boomer-config so the boomer-worker workers can fetch
    # runtime flag values (trace probability, wait times) the operator set
    # in the web UI form. No-op on workers without a web UI. Required here,
    # not merely useful: the boomer container runs with --master-web-port,
    # and a failed fetch is fatal to that worker.
    init_boomer_config()

    class RunWorkloadUser(User):
        host = "api.ate-system.svc.cluster.local:443"

        @task
        def noop(self) -> None:
            # Unreached under normal operation: the Python worker container
            # does not load this file (LOCUST_NO_RUNWORKLOAD_USER=1). Body is
            # required because locust validates that every User has at least
            # one @task method.
            pass
