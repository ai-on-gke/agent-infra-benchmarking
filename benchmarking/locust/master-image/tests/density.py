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

"""Stub DensityUser declaration, on top of what _master does for every user
class.

The real load implementation lives in the boomer-Go worker at
benchmarking/locust/go/internal/density/; this Python class is declared only
so the master's --class-picker enumerates the name. A Python worker container
would set LOCUST_NO_DENSITY_USER=1 to skip it, leaving boomer as the sole owner
of DensityUser load.

Its name is load-bearing twice over: the master loads
/app/tests/<user-class>.py, and the boomer worker is passed
--user-class=<user-class>, which selects density in its main.go.
"""

import os

# Importable as locust puts this file's directory on sys.path while loading it.
import _master  # noqa: F401

if os.environ.get("LOCUST_NO_DENSITY_USER") != "1":
    from locust import User, task

    class DensityUser(User):
        host = "api.ate-system.svc.cluster.local:443"

        @task
        def noop(self) -> None:
            # Unreached under normal operation: the Python worker container
            # does not load this file (LOCUST_NO_DENSITY_USER=1). Body is
            # required because locust validates that every User has at least
            # one @task method.
            pass
