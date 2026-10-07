#!/usr/bin/env python3

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

"""Runs every case in tests.yaml, one after another, in the order listed."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

TESTS_FILE = Path(__file__).resolve().parent / "tests.yaml"


def load_cases(tests_file: Path) -> dict[str, dict]:
    with tests_file.open() as f:
        cases = (yaml.safe_load(f) or {}).get("cases")
    if not isinstance(cases, dict) or not cases:
        raise ValueError(f"{tests_file} has no `cases` mapping")
    for name, case in cases.items():
        if not isinstance(case, dict):
            raise ValueError(f"{tests_file}: case {name} is not a mapping")
    return cases


def run_case(name: str, case: dict) -> None:
    # A no-op for now.
    print(f"{name}: {json.dumps(case)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tests-file", type=Path, default=TESTS_FILE, help=f"default: {TESTS_FILE}")
    args = p.parse_args()

    try:
        cases = load_cases(args.tests_file)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.exit(f"run_tests: {exc}")

    for name, case in cases.items():
        run_case(name, case)


if __name__ == "__main__":
    main()
