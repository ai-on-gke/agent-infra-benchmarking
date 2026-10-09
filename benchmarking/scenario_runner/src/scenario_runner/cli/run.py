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

"""Applies scenario_runner/infra/, runs the cases in scenarios.yaml one after
another (every case in the order listed, or the ones --scenario names in the
order given), each of a case's repeats on its own infra/clusters/ cluster
applied for it and destroyed after it, and destroys the infra again. With
--dev-infra, uses the scenario_runner/infra and the cluster of that dev infra
instead, which outlive the execution."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NoReturn

import yaml

from .. import LOCAL_DATA_DIR, benchmark, dev_infra, names, substrate
from ..cases import SKIP_SUBSTRATE_INSTALL, SCENARIOS_FILE, load_cases, record_names
from ..cluster import Cluster
from ..deployment import RUNNER_INFRA_DIR, Deployment, GcsDeployment, cluster_deployment
from ..log import log
from ..process import failure

# The executions' Deployments, kept after they are destroyed, and with gcs in
# the state bucket under <state prefix>/ too. An execution's are in <name>/:
# scenario-runner/ for scenario_runner/infra, and scenarios/<record>/ for each
# repeat's cluster, <record> being what the repeat is recorded under.
LOCAL_TERRAFORM_DIR = LOCAL_DATA_DIR / "runs" / "terraform"

STATE_BACKENDS = ["local", "gcs"]

# What a failed repeat raises, its infra's apply included.
RUN_ERRORS = (subprocess.CalledProcessError, *benchmark.ERRORS, substrate.SubstrateError)


def fail(message: str) -> NoReturn:
    log(f"run: {message}", "ERROR")
    sys.exit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python3 -m scenario_runner.cli.run", description=__doc__)
    p.add_argument("--scenarios-file", type=Path, default=SCENARIOS_FILE, help=f"default: {SCENARIOS_FILE}")
    p.add_argument(
        "--scenario",
        action="append",
        metavar="NAME",
        help="a case to run; repeat it for more. Default: every case",
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
        help="use the benchmark images already pushed for this commit rather than building them; only with --dev-infra",
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
    names = list(dict.fromkeys(args.scenario or cases))
    unknown = [name for name in names if name not in cases]
    if unknown:
        fail(f"no case {', '.join(unknown)} in {args.scenarios_file}; it has {', '.join(cases)}")
    return {name: cases[name] for name in names}


def check_requirements(cases: dict[str, dict], cloud_build: bool) -> None:
    if not shutil.which("terraform"):
        fail("terraform is required to apply the infra")
    if not all(case.get(SKIP_SUBSTRATE_INSTALL) for case in cases.values()):
        missing = substrate.missing_requirements()
        if missing:
            fail(f"installing substrate requires {', '.join(missing)}")
        # Its cluster's nodes are labeled with it.
        try:
            substrate.build_version()
        except substrate.SubstrateError as exc:
            fail(str(exc))
    benchmarks = {name: case for name, case in cases.items() if case["type"] != "noop"}
    missing = benchmark.missing_requirements(benchmarks, cloud_build)
    if missing:
        fail(f"running the benchmark requires {', '.join(missing)}")


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
            SKIP_SUBSTRATE_INSTALL: cases[first].get(SKIP_SUBSTRATE_INSTALL, False),
        }
    for name, case in cases.items():
        spec = case["infra"]["cluster"]
        if spec["name"] != record["cluster"]["name"]:
            fail(
                f"case {name} runs on infra/clusters/{spec['name']}, but dev infra {args.dev_infra}"
                f" is infra/clusters/{record['cluster']['name']}"
            )
        if spec != record["cluster"]:
            log(f"run: case {name}'s infra.cluster differs from dev infra {args.dev_infra}'s it runs on", "WARNING")
    if args.dev_infra_update:
        log(f"run: updating dev infra {args.dev_infra} from the source")
        record = dev_infra.apply(record, args.terraform_output == "json", update=True)
    elif not record.get("complete"):
        log(f"run: applying dev infra {args.dev_infra}, which is not complete")
        record = dev_infra.apply(record, args.terraform_output == "json")
    return record


def run_case(args: argparse.Namespace, name: str, case: dict, cluster: Cluster) -> None:
    if case["type"] == "noop":
        log(f"{name}: noop, nothing to run")
        return
    benchmark.run(
        name,
        case,
        args.name,
        cluster,
        cloud_build=args.cloud_build,
        docker_login=not args.no_docker_login,
        build=not args.no_build,
        deploy_workloads=not args.no_deploy_workloads,
    )


def run_repeats(name: str, case: dict, run: Callable[[str], None]) -> None:
    """Calls run with each of the case's record names. A failed repeat does not
    stop the ones after it; the case fails once they have run."""
    records = record_names(name, case)
    if len(records) == 1:
        run(name)
        return
    failed = []
    for n, record in enumerate(records, 1):
        log(f"{name}: repeat {n} of {len(records)}, as {record}")
        try:
            run(record)
        except RUN_ERRORS as exc:
            log(f"{record}: {failure(exc) if isinstance(exc, subprocess.CalledProcessError) else exc}", "ERROR")
            failed.append(record)
    if failed:
        fail(f"{len(failed)} of {len(records)} repeats of {name} failed: {', '.join(failed)}")


def run_on_dev_infra(args: argparse.Namespace, cases: dict[str, dict]) -> None:
    """A case's repeats run one after another on the dev infra's cluster."""
    record = prepare_dev_infra(args, cases)
    cluster = dev_infra.cluster(args.dev_infra)
    for name, case in cases.items():
        if not case.get(SKIP_SUBSTRATE_INSTALL):
            record = dev_infra.install_substrate(record)
        run_repeats(name, case, lambda run_name: run_case(args, run_name, case, cluster))


def run_on_own_infra(args: argparse.Namespace, cases: dict[str, dict]) -> None:
    """Each of a case's repeats runs on a cluster of its own, applied for it and
    destroyed after it."""
    # Every repeat's, before anything is applied, so a name that can't be fails first.
    case_vars = {
        record: names.infra_vars(args.project_id, args.region, args.name, record)
        for name, case in cases.items()
        for record in record_names(name, case)
    }
    variables = names.infra_vars(args.project_id, args.region, args.name)
    runner_infra = deployment(args, "scenario-runner").create(RUNNER_INFRA_DIR, variables)

    def run_on_cluster(case: dict, record: str) -> None:
        version = None if case.get(SKIP_SUBSTRATE_INSTALL) else substrate.build_version()
        uncreated = deployment(args, "scenarios", record)
        case_infra = cluster_deployment(case["infra"]["cluster"], uncreated, case_vars[record], version)
        with case_infra.applied():
            cluster = Cluster(case_infra.outputs(), case_infra.data / "kubeconfig")
            if not case.get(SKIP_SUBSTRATE_INSTALL):
                substrate.install(cluster)
            run_case(args, record, case, cluster)

    with runner_infra.applied():
        for name, case in cases.items():
            run_repeats(name, case, lambda record: run_on_cluster(case, record))


def main() -> None:
    args = parse_args()
    cases = selected_cases(args)
    check_requirements(cases, args.cloud_build)

    # Cloud Run stops a task that outlives its timeout with SIGTERM; exiting
    # on it, rather than dying, still destroys the infra.
    signal.signal(signal.SIGTERM, lambda *_: fail("stopped by SIGTERM"))

    try:
        if args.dev_infra:
            run_on_dev_infra(args, cases)
        else:
            run_on_own_infra(args, cases)
    except subprocess.CalledProcessError as exc:
        fail(failure(exc))
    except (*RUN_ERRORS, names.NamingError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
