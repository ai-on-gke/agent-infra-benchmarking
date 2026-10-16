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

"""Tests of the master image's code, run with its requirements installed:

    python -m unittest discover -s benchmarking/locust/master-image/unit_tests -t benchmarking/locust/master-image

Not copied into the image; tests/ holds the locustfile, not tests."""

import sys
from pathlib import Path

# The image runs its modules from one flat directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
