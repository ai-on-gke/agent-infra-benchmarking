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

"""Prints one case of tests.yaml as JSON, or the name of every case.

run_locust_job.sh turns the case into the two Jobs' arguments, so neither
image reads tests.yaml. An unknown case is a non-zero exit naming the ones the
file does hold.
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "tests.yaml"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-f", default=str(DEFAULT_PATH), help="Path to the tests file.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--case", help="Case to print as JSON.")
    g.add_argument("--list", action="store_true", help="Print every case name, one per line.")
    args = p.parse_args()

    try:
        import yaml
    except ModuleNotFoundError:
        sys.exit("test_case.py needs PyYAML: pip install pyyaml")

    try:
        with open(args.f) as f:
            doc = yaml.safe_load(f) or {}
    except OSError as e:
        sys.exit(f"Cannot read {args.f}: {e}")
    except yaml.YAMLError as e:
        sys.exit(f"{args.f} is not valid YAML: {e}")

    cases = doc.get("cases")
    if not isinstance(cases, dict):
        sys.exit(f"{args.f} has no `cases` mapping.")

    if args.list:
        print("\n".join(sorted(cases)))
        return
    if args.case not in cases:
        sys.exit(f"No test case {args.case!r} in {args.f}. Known: {', '.join(sorted(cases)) or '(none)'}")
    case = cases[args.case] or {}
    if not isinstance(case, dict):
        sys.exit(f"Test case {args.case!r} in {args.f} is not a mapping of flags.")
    json.dump(case, sys.stdout)
    print()


if __name__ == "__main__":
    main()
