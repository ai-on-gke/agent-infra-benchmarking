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

"""Builds the images the scenario-runner runs from this tree into a
repository, which its --image-repo then names, or a dev infra's: substrate's,
env's, agent-sandbox's and the benchmark's own. Each is built only if the
repository does not have it at its tag yet, except a submodule's at a -dirty
version, which is always built again. A run from a machine builds the ones
it lacks itself, so this is for building ahead of one, and for CI."""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import NoReturn

from .. import builder, dev_infra, images, substrate
from ..log import log
from ..process import failure


def fail(message: str) -> NoReturn:
    log(f"build-images: {message}", "ERROR")
    sys.exit(1)


def groups(value: str) -> list[str]:
    chosen = [group.strip() for group in value.split(",") if group.strip()]
    unknown = [group for group in chosen if group not in builder.GROUPS]
    if unknown or not chosen:
        raise argparse.ArgumentTypeError(f"{value!r}: name some of {', '.join(builder.GROUPS)}, comma-separated")
    return list(dict.fromkeys(chosen))


def docker_repo(value: str) -> str:
    try:
        images.repository_name(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return value


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python3 -m scenario_runner.cli.build_images", description=__doc__)
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--repo", type=docker_repo, help="the repository's image path prefix, <location>-docker.pkg.dev/<project>/<name>"
    )
    where.add_argument(
        "--dev-infra", type=dev_infra.check_name, metavar="NAME", help="the repository of this dev infra, once applied"
    )
    p.add_argument(
        "--images",
        type=groups,
        default=list(builder.GROUPS),
        metavar="GROUPS",
        help=f"which of {', '.join(builder.GROUPS)} to build, comma-separated. Default: all",
    )
    p.add_argument(
        "--no-docker-login",
        action="store_true",
        help="leave a local build's registry credentials to Docker's own configuration, rather than logging in"
        " with gcloud's access token",
    )
    for flag in ("--cache-from", "--cache-to"):
        p.add_argument(
            flag,
            action="append",
            default=[],
            metavar="SPEC",
            help=f"docker build's {flag} for each image Docker builds, {{image}} in it as the image's name, such as"
            " type=gha,scope={image}; repeatable. ko's builds take none",
        )
    p.add_argument("--force", action="store_true", help="build every image, even those the repository has")
    p.add_argument("--dry-run", action="store_true", help="only say which images would be built")
    args = p.parse_args()
    if args.dev_infra:
        record = dev_infra.read_record(args.dev_infra)
        outputs = dev_infra.runner_outputs(args.dev_infra) if record else {}
        if not outputs.get("docker_repo"):
            p.error(f"--dev-infra: dev infra {args.dev_infra} has no repository yet; run it once to apply it")
        args.repo = outputs["docker_repo"]
    return args


def main() -> None:
    args = parse_args()
    cache_args = [f"--cache-from={spec}" for spec in args.cache_from] + [f"--cache-to={spec}" for spec in args.cache_to]
    missing = builder.missing_requirements(args.images)
    if missing:
        fail(f"building {', '.join(args.images)} requires {', '.join(missing)}")
    try:
        if "substrate" in args.images:
            substrate.check_pinned()
        # Every tag before any build, which could leave files that change one.
        planned = {group: (builder.refs(group, args.repo), builder.dirty(group)) for group in args.images}
        for group, (group_refs, dirty) in planned.items():
            if args.force or dirty:
                wanted = group_refs
            else:
                wanted = images.missing(group_refs)
            if not wanted:
                log(f"build-images: {group}: {args.repo} has every image already")
                continue
            listed = "\n".join(f"  {ref}" for ref in wanted)
            if args.dry_run:
                log(f"build-images: {group}: would build\n{listed}")
                continue
            log(f"build-images: {group}: building\n{listed}")
            builder.build(group, args.repo, wanted, not args.no_docker_login, cache_args)
    except subprocess.CalledProcessError as exc:
        fail(failure(exc))
    except (images.ImageError, substrate.SubstrateError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
