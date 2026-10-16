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

"""Applies scenario_runner/infra/, builds the benchmark's images into it once,
runs the cases in scenarios.yaml one after another (the ones --scenario names
in the order given, or with --all every case in the order listed), each of a
case's repeats on its own infra/clusters/ cluster applied for it and destroyed
after it, and destroys the infra again. With --dev-infra, uses the
scenario_runner/infra and the cluster of that dev infra instead, which outlive
the execution. Before any of it, checks what the cases need."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NoReturn

import yaml

from .. import LOCAL_DATA_DIR, benchmark, dev_infra, images, locust, names, preflight, substrate, summary
from ..cases import SKIP_SUBSTRATE_INSTALL, SCENARIOS_FILE, load_cases, record_names
from ..cluster import Cluster
from ..deployment import RUNNER_INFRA_DIR, Deployment, DestroyFailed, GcsDeployment, cluster_deployment
from ..log import ON_CLOUD_RUN, log
from ..process import failure

# The executions' Deployments, kept after they are destroyed, and with gcs in
# the state bucket under <state prefix>/ too. An execution's are in <name>/:
# scenario-runner/ for scenario_runner/infra, and scenarios/<record>/ for each
# repeat's cluster, <record> being what the repeat is recorded under.
LOCAL_TERRAFORM_DIR = LOCAL_DATA_DIR / "runs" / "terraform"

STATE_BACKENDS = ["local", "gcs"]

# What a failed repeat raises, its infra's apply included.
RUN_ERRORS = (subprocess.CalledProcessError, *benchmark.ERRORS, substrate.SubstrateError)


class CaseFailed(RuntimeError):
    """Some of a case's repeats failed; the cases after it still run."""


def fail(message: str) -> NoReturn:
    log(f"run: {message}", "ERROR")
    sys.exit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python3 -m scenario_runner.cli.run", description=__doc__)
    p.add_argument("--scenarios-file", type=Path, default=SCENARIOS_FILE, help=f"default: {SCENARIOS_FILE}")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--scenario", action="append", metavar="NAME", help="a case to run; repeat it for more")
    which.add_argument(
        "--all",
        action="store_true",
        help="run every case in the scenarios file, in the order listed, each on clusters of its own",
    )
    p.add_argument(
        "--project-id",
        default=os.environ.get("PROJECT_ID"),
        required="PROJECT_ID" not in os.environ,
        help="GCP project to create the infra in. Default: $PROJECT_ID, which benchmarking/.env.sh can set",
    )
    p.add_argument("--region", default="us-central1", help="default: %(default)s")
    p.add_argument(
        "--state-backend",
        choices=STATE_BACKENDS,
        default=os.environ.get("STATE_BACKEND"),
        required="STATE_BACKEND" not in os.environ,
        help="where the infra's Terraform state goes: a local file or a GCS bucket. Default: $STATE_BACKEND, which"
        " benchmarking/.env.sh can set",
    )
    p.add_argument(
        "--state-bucket",
        help="GCS bucket holding the state; only, and required, with gcs. Default: $STATE_BUCKET",
    )
    p.add_argument(
        "--state-prefix",
        help="prefix in the state bucket, each execution's Terraform deployments being under <prefix>/<name>; only,"
        f" and required, with gcs. With local, they are only under {LOCAL_TERRAFORM_DIR}/<name>."
        " Default: $STATE_PREFIX",
    )
    p.add_argument(
        "--report-bucket",
        help="GCS bucket the cases' reports go to, under --report-prefix. Default: each case's cluster's own"
        " bucket, which is destroyed with it",
    )
    p.add_argument(
        "--report-prefix",
        default=locust.REPORT_PREFIX,
        help="folder in the report bucket the reports go under, as <prefix>/<name>/<record>/. Default: %(default)s",
    )
    p.add_argument(
        "--name",
        type=benchmark.check_run_id,
        default=datetime.now(timezone.utc).strftime("run-%Y%m%dt%H%M%Sz"),
        help="this execution's run id and state prefix, which its infra's names start with; unique to it."
        " Default: run-<UTC timestamp>",
    )
    p.add_argument(
        "--terraform-output",
        choices=["text", "json"],
        default="text",
        help="how terraform plan and apply log: their text output line by line, or -json one message per entry."
        " Default: %(default)s",
    )
    p.add_argument(
        "--dev-infra",
        type=dev_infra.check_name,
        metavar="NAME",
        help="run every case on this dev infra, a scenario_runner/infra and a cluster kept until"
        " python3 -m scenario_runner.cli.dev_infra destroys them, rather than on ones applied for this execution."
        " Applied unless complete, a new one with the first case's cluster",
    )
    p.add_argument(
        "--dev-infra-update",
        action="store_true",
        help="copy the dev infra's configs from the source again, with the first case's cluster, and apply them even"
        " if it is complete, reinstalling substrate if its version changed; only with --dev-infra",
    )
    p.add_argument(
        "--cloud-build",
        action="store_true",
        help="build the benchmark images on Cloud Build rather than with local Docker",
    )
    p.add_argument(
        "--no-docker-login",
        action="store_true",
        help="leave a local build's registry credentials to Docker's own configuration, rather than logging in"
        " with gcloud's access token",
    )
    p.add_argument(
        "--no-build",
        action="store_true",
        help="use the benchmark images already pushed for this tree, uncommitted changes included, rather than"
        " building them; only with --dev-infra",
    )
    p.add_argument(
        "--no-deploy-workloads",
        action="store_true",
        help="use the mock-llm and ActorTemplates already deployed rather than deploying the case's; only with"
        " --dev-infra. The worker pool is recreated either way",
    )
    args = p.parse_args()
    # argparse checks choices only against what is given, not the default.
    if args.state_backend not in STATE_BACKENDS:
        p.error(f"STATE_BACKEND must be one of {', '.join(STATE_BACKENDS)}, not {args.state_backend!r}")
    # Taken from the environment only with gcs, so a .env.sh set up for gcs
    # still lets --state-backend=local run.
    if args.state_backend == "gcs":
        args.state_bucket = args.state_bucket or os.environ.get("STATE_BUCKET")
        args.state_prefix = args.state_prefix or os.environ.get("STATE_PREFIX")
    # A cluster applied for the execution has neither yet.
    if not args.dev_infra and (args.no_build or args.no_deploy_workloads or args.dev_infra_update):
        p.error("--no-build, --no-deploy-workloads and --dev-infra-update are only for --dev-infra")
    if not re.fullmatch(r"[^/]+(/[^/]+)*", args.report_prefix):
        p.error("--report-prefix must not be empty, nor start or end with a slash")
    if args.state_backend == "gcs" and not (args.state_bucket and args.state_prefix):
        p.error("--state-backend=gcs requires --state-bucket and --state-prefix, or STATE_BUCKET and STATE_PREFIX")
    if args.state_backend == "local" and (args.state_bucket or args.state_prefix):
        p.error("--state-bucket and --state-prefix are only for --state-backend=gcs")
    return args


def selected_cases(args: argparse.Namespace) -> dict[str, dict]:
    """The cases to run, by name, in the order they run."""
    try:
        cases = load_cases(args.scenarios_file)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        fail(str(exc))
    # dict.fromkeys drops repeated names, keeping the first.
    names = list(dict.fromkeys(cases if args.all else args.scenario))
    unknown = [name for name in names if name not in cases]
    if unknown:
        fail(f"no case {', '.join(unknown)} in {args.scenarios_file}; it has {', '.join(cases)}")
    return {name: cases[name] for name in names}


def applied_clusters(args: argparse.Namespace, cases: dict[str, dict]) -> list[str]:
    """The cases whose cluster the run applies: each one's, on clusters of
    their own; on a dev infra, the first one's if it creates, completes or
    updates the dev infra, and none if the dev infra is complete."""
    if not args.dev_infra:
        return list(cases)
    record = dev_infra.read_record(args.dev_infra)
    if record is None or args.dev_infra_update or not record.get("complete"):
        return [next(iter(cases))]
    return []


def check_requirements(args: argparse.Namespace, cases: dict[str, dict]) -> None:
    """Fails, naming every problem, before anything is applied."""
    log(f"run: checking what {', '.join(cases)} need before applying anything")
    problems = preflight.check(cases, args.project_id, args.region, args.cloud_build, applied_clusters(args, cases))
    if problems:
        fail("cannot run, as:\n" + "\n".join(f"  - {problem}" for problem in problems))
    if args.report_bucket or all(case["type"] == "noop" for case in cases.values()):
        return
    if args.dev_infra:
        log(
            f"run: no --report-bucket, so the reports go to dev infra {args.dev_infra}'s cluster bucket, which goes"
            f" when the dev infra is destroyed, and are downloaded to {locust.REPORTS_DIR / args.name}"
        )
        return
    log(
        "run: no --report-bucket, so each case's reports go to its cluster's own bucket, which is destroyed with"
        f" it; the only copy that lasts is the one downloaded to {locust.REPORTS_DIR / args.name}",
        "WARNING",
    )


def build_images(args: argparse.Namespace, cases: dict[str, dict], repo: str) -> None:
    """Builds the benchmark's images into the runner infra's repository once,
    for every case and repeat to pull from."""
    benchmarks = [case for case in cases.values() if case["type"] != "noop"]
    if not benchmarks:
        return
    if args.no_build:
        images.check_pushed(repo, [images.LOCUST_IMAGES, images.WORKLOADS_IMAGES])
        log(f"run: using the images already in {repo} for {images.tag()}")
        return
    images.build_and_push(
        repo,
        args.project_id,
        [images.LOCUST_IMAGES, images.WORKLOADS_IMAGES],
        cloud_build=args.cloud_build,
        login=not args.no_docker_login,
    )


def deployment(args: argparse.Namespace, *key: str) -> Deployment:
    """The deployment of an infra of this execution, in
    .local-data/runs/terraform/<name>/<key>/, and with gcs under
    <state prefix>/<name>/<key> in the state bucket too."""
    root = LOCAL_TERRAFORM_DIR.joinpath(args.name, *key)
    json_output = args.terraform_output == "json"
    if args.state_backend == "gcs":
        prefix = "/".join([args.state_prefix, args.name, *key])
        return GcsDeployment(root, json_output, bucket=args.state_bucket, prefix=prefix)
    return Deployment(root, json_output)


def prepare_dev_infra(args: argparse.Namespace, cases: dict[str, dict]) -> dict:
    """Checks every case can run on the dev infra --dev-infra names, applies it
    unless it is complete or with --dev-infra-update, and returns its record. A
    new or updated one has the first case's cluster."""
    record = dev_infra.read_record(args.dev_infra)
    first = next(iter(cases))
    if record is not None:
        if (record["project_id"], record["region"]) != (args.project_id, args.region):
            fail(
                f"dev infra {args.dev_infra} is in project {record['project_id']} and region {record['region']},"
                f" not {args.project_id} and {args.region}"
            )
        # Another config under the same state would replace every resource.
        if args.dev_infra_update and cases[first]["infra"]["cluster"]["name"] != record["cluster"]["name"]:
            fail(
                f"case {first} runs on infra/clusters/{cases[first]['infra']['cluster']['name']}, but dev infra"
                f" {args.dev_infra} is infra/clusters/{record['cluster']['name']}; destroy it rather than update it"
            )
    if record is None or args.dev_infra_update:
        record = {
            **(record or {}),
            "name": args.dev_infra,
            "test_case": first,
            "project_id": args.project_id,
            "region": args.region,
            "cluster": cases[first]["infra"]["cluster"],
            "sandbox_class": sandbox_class(cases[first]),
            SKIP_SUBSTRATE_INSTALL: cases[first].get(SKIP_SUBSTRATE_INSTALL, False),
        }
    for name, case in cases.items():
        spec = case["infra"]["cluster"]
        if spec["name"] != record["cluster"]["name"]:
            fail(
                f"case {name} runs on infra/clusters/{spec['name']}, but dev infra {args.dev_infra}"
                f" is infra/clusters/{record['cluster']['name']}"
            )
        # The sandboxed pool is tainted for one class, which no other's workers tolerate.
        if case["type"] != "noop" and record.get("sandbox_class") and sandbox_class(case) != record["sandbox_class"]:
            fail(
                f"case {name} is {sandbox_class(case)}, but dev infra {args.dev_infra}'s sandboxed pool is"
                f" {record['sandbox_class']}; use a dev infra of its own"
            )
        if spec != record["cluster"]:
            log(f"run: case {name}'s infra.cluster differs from dev infra {args.dev_infra}'s it runs on", "WARNING")
    json_output = args.terraform_output == "json"
    login = not args.no_docker_login
    if args.dev_infra_update:
        log(f"run: updating dev infra {args.dev_infra} from the source")
        record = dev_infra.apply(record, json_output, update=True, cloud_build=args.cloud_build, login=login)
    elif not record.get("complete"):
        log(f"run: applying dev infra {args.dev_infra}, which is not complete")
        record = dev_infra.apply(record, json_output, cloud_build=args.cloud_build, login=login)
    return record


def sandbox_class(case: dict) -> str | None:
    """The class of the case's workers, which its cluster's sandboxed pool is
    applied for; None for a noop."""
    return None if case["type"] == "noop" else case.get("sandbox-class", "gvisor")


def run_case(
    args: argparse.Namespace, record: str, case: dict, cluster: Cluster, name: str, repeat: int, image_repo: str
) -> None:
    if case["type"] == "noop":
        log(f"{record}: noop, nothing to run")
        return
    benchmark.run(
        record,
        case,
        args.name,
        cluster,
        image_repo=image_repo,
        cloud_build=args.cloud_build,
        deploy_workloads=not args.no_deploy_workloads,
        test_case=name,
        repeat=repeat,
        report_bucket=args.report_bucket,
        report_prefix=args.report_prefix,
        keeps_bucket=bool(args.dev_infra),
    )


def describe(exc: BaseException) -> str:
    return failure(exc) if isinstance(exc, subprocess.CalledProcessError) else str(exc)


def run_repeats(name: str, case: dict, run: Callable[[str, int], None], shared_cluster: bool) -> None:
    """Calls run with each of the case's record names and repeat numbers, 0 for
    a case run once. A failed repeat does not stop the ones after it; the case
    fails once they have run. A cluster that could not be destroyed does, and
    on a shared_cluster a run that may still be going, as run_cases says."""
    records = record_names(name, case)
    if len(records) == 1:
        run(name, 0)
        return
    failed = []
    for n, record in enumerate(records, 1):
        log(f"{name}: repeat {n} of {len(records)}, as {record}")
        try:
            run(record, n)
        except locust.LocustStillRunning as exc:
            if shared_cluster:
                raise
            log(f"{record}: {exc}", "ERROR")
            failed.append(record)
        except RUN_ERRORS as exc:
            log(f"{record}: {describe(exc)}", "ERROR")
            failed.append(record)
    if failed:
        raise CaseFailed(f"{len(failed)} of {len(records)} repeats of {name} failed: {', '.join(failed)}")


def run_cases(
    args: argparse.Namespace, cases: dict[str, dict], run: Callable[[str, dict], None], shared_cluster: bool
) -> list[str]:
    """Calls run with each case, and returns the ones that failed. A failed case
    does not stop the ones after it; an interrupt, or a cluster that could not
    be destroyed, does. So does a run that may still be going on a
    shared_cluster, which the next case would deploy under it; on a cluster of
    its own, which is destroyed after it, the next case runs."""
    failed = []
    for name, case in cases.items():
        try:
            run(name, case)
        except locust.LocustStillRunning as exc:
            if shared_cluster:
                raise
            log(f"{name}: {exc}", "ERROR")
            failed.append(name)
        except (CaseFailed, *RUN_ERRORS) as exc:
            log(f"{name}: {describe(exc)}", "ERROR")
            failed.append(name)
        # From what was downloaded, so not on Cloud Run.
        records = record_names(name, case)
        if len(records) > 1 and case["type"] != "noop" and not ON_CLOUD_RUN:
            summary.write(locust.REPORTS_DIR / args.name, name, records)
    return failed


def run_on_dev_infra(args: argparse.Namespace, cases: dict[str, dict]) -> list[str]:
    """A case's repeats run one after another on the dev infra's cluster.
    Returns the cases that failed."""
    record = prepare_dev_infra(args, cases)
    cluster = dev_infra.cluster(args.dev_infra)
    image_repo = dev_infra.runner_outputs(args.dev_infra)["docker_repo"]
    build_images(args, cases, image_repo)

    def run(name: str, case: dict) -> None:
        nonlocal record
        if not case.get(SKIP_SUBSTRATE_INSTALL):
            record = dev_infra.install_substrate(record, args.cloud_build, not args.no_docker_login)
        run_repeats(
            name,
            case,
            lambda run_name, repeat: run_case(args, run_name, case, cluster, name, repeat, image_repo),
            shared_cluster=True,
        )

    return run_cases(args, cases, run, shared_cluster=True)


def run_on_own_infra(args: argparse.Namespace, cases: dict[str, dict]) -> list[str]:
    """Each of a case's repeats runs on a cluster of its own, applied for it and
    destroyed after it. Returns the cases that failed."""
    # Every repeat's, before anything is applied, so a name that can't be fails first.
    case_vars = {
        record: names.infra_vars(args.project_id, args.region, args.name, record)
        for name, case in cases.items()
        for record in record_names(name, case)
    }
    variables = {**names.infra_vars(args.project_id, args.region, args.name), "image_writers": images.writers()}
    runner_infra = deployment(args, "scenario-runner").create(RUNNER_INFRA_DIR, variables)

    def run_on_cluster(name: str, case: dict, record: str, repeat: int, outputs: dict) -> None:
        version = None if case.get(SKIP_SUBSTRATE_INSTALL) else substrate.build_version()
        uncreated = deployment(args, "scenarios", record)
        spec = case["infra"]["cluster"]
        # A cluster that pulls none of the runner infra's images is not granted it.
        pulls = version is not None or case["type"] != "noop"
        repository = outputs["artifact_registry_repository"] if pulls else None
        case_infra = cluster_deployment(spec, uncreated, case_vars[record], version, repository, sandbox_class(case))
        with case_infra.applied():
            cluster = Cluster(case_infra.outputs(), case_infra.data / "kubeconfig")
            try:
                if not case.get(SKIP_SUBSTRATE_INSTALL):
                    substrate.install(cluster)
                run_case(args, record, case, cluster, name, repeat, outputs["docker_repo"])
            finally:
                if pulls:
                    cluster.delete_volumes()

    with runner_infra.applied():
        outputs = runner_infra.outputs()
        # Once, into the runner infra's repository, which every case's cluster
        # installs substrate from and pulls the benchmark's images from.
        if not all(case.get(SKIP_SUBSTRATE_INSTALL) for case in cases.values()):
            login = not args.no_docker_login
            substrate.build_images(outputs["docker_repo"], args.project_id, args.cloud_build, login)
        build_images(args, cases, outputs["docker_repo"])
        return run_cases(
            args,
            cases,
            lambda name, case: run_repeats(
                name,
                case,
                lambda record, repeat: run_on_cluster(name, case, record, repeat, outputs),
                shared_cluster=False,
            ),
            shared_cluster=False,
        )


def main() -> None:
    args = parse_args()
    cases = selected_cases(args)
    check_requirements(args, cases)

    # Cloud Run stops a task that outlives its timeout with SIGTERM; exiting
    # on it, rather than dying, still destroys the infra.
    signal.signal(signal.SIGTERM, lambda *_: fail("stopped by SIGTERM"))

    try:
        if args.dev_infra:
            failed = run_on_dev_infra(args, cases)
        else:
            failed = run_on_own_infra(args, cases)
    except KeyboardInterrupt:
        # The infra applied for the execution is destroyed on the way out, but
        # an interrupt during that destroy leaves some of it behind.
        where = f"its state is in {LOCAL_TERRAFORM_DIR / args.name}" + (
            f" and gs://{args.state_bucket}/{args.state_prefix}/{args.name}" if args.state_backend == "gcs" else ""
        )
        log(f"run: interrupted. If a destroy was cut short, what it left is still up; {where}", "ERROR")
        sys.exit(130)
    except subprocess.CalledProcessError as exc:
        fail(failure(exc))
    except (*RUN_ERRORS, names.NamingError, DestroyFailed) as exc:
        fail(str(exc))
    if failed:
        fail(f"{len(failed)} of {len(cases)} cases failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
