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

"""Prints cases as the scenario-runner runs them: with the templates they
extend merged in and each subcase a case of its own. A case with subcases
prints them all; no case named prints every one."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from ..cases import SCENARIOS_FILE, grouped, load_cases
from ..log import log


def main() -> None:
    p = argparse.ArgumentParser(prog="python3 -m scenario_runner.cli.cases", description=__doc__)
    p.add_argument("--scenarios-file", type=Path, default=SCENARIOS_FILE, help=f"default: {SCENARIOS_FILE}")
    p.add_argument("case", nargs="*", help="a case, or one with subcases")
    args = p.parse_args()
    try:
        cases = load_cases(args.scenarios_file)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        log(f"cases: {exc}", "ERROR")
        sys.exit(1)
    groups = grouped(cases)
    unknown = [name for name in args.case if name not in cases and name not in groups]
    if unknown:
        log(f"cases: no case {', '.join(unknown)} in {args.scenarios_file}", "ERROR")
        sys.exit(1)
    names = [member for name in args.case for member in groups.get(name, [name])] or list(cases)
    yaml.safe_dump({name: cases[name] for name in names}, sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()
