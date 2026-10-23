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

"""Runs the cases in scenarios.yaml one after another (the ones --scenario
names in the order given, the ones a --suite lists, or with --all every case in
the order listed), each
case's warm-ups and repeats on an infra/clusters/ cluster applied for it and
destroyed after them, or with `repeats.cluster: per-repeat` each repeat on one
of its own; a case's subcases one after another, those alike in their cluster
on one, then its type's summarize of them all; pulling the images
benchmarking/tools/build-images built into
--image-repo. From a machine rather
than Cloud Run, it builds the images the repository lacks itself, and without
--image-repo applies a repository for the execution, scenario_runner/infra,
destroyed after its clusters. With --dev-infra, uses the cluster of that dev
infra instead, and its repository, which outlive the execution. Before any of
it, checks what the cases need, the images included."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable, Iterator, NoReturn

import yaml

from .. import LABEL_RE, LOCAL_DATA_DIR, benchmarks, images, preflight, reports
from ..benchmarks import noop, sandbox_benchmark
from ..benchmarks.sandbox_benchmark import ate_env
from ..control_planes import agent_sandbox, substrate
from ..images import builder
from ..infra import dev_infra, names, terraform
from ..infra.cluster import Cluster, gke_stockout, utc_now
from ..infra.cluster_configs import execution, from_record, to_record
from ..infra.deployment import RUNNER_INFRA_DIR, Deployment, DestroyFailed, GcsDeployment
from ..log import ON_CLOUD_RUN, log
from ..process import failure
from ..reports import summary
from ..scenarios.bare_metal import BARE_METAL
from ..scenarios.batches import batch_key, batches, cluster_keys
from ..scenarios.expand import grouped
from ..scenarios.file import SCENARIOS_FILE, load_scenarios
from ..scenarios.install import AGENT_SANDBOX, INSTALL, SANDBOX_CLASS, SUBSTRATE, control_plane, sandbox_class
from ..scenarios.repeats import record_names, run_names, shares_cluster, warmup_names
from ..scenarios.select import select

# The executions' Deployments, kept after they are destroyed, and with gcs in
# the state bucket under <state prefix>/ too. An execution's are in <name>/:
# scenarios/<record>/ for each repeat's cluster, <record> being what the repeat
# is recorded under, or scenarios/<case>/ for a case whose repeats share one.
# One from a machine without --image-repo, or from before the image repository
# outlived it, also has scenario-runner/, for scenario_runner/infra.
LOCAL_TERRAFORM_DIR = LOCAL_DATA_DIR / "runs" / "terraform"

STATE_BACKENDS = ["local", "gcs"]

# What a failed repeat raises, its infra's apply included.
RUN_ERRORS = (subprocess.CalledProcessError, *benchmarks.ERRORS, substrate.SubstrateError, agent_sandbox.AgentSandboxError)


class CaseFailed(RuntimeError):
    """Some of a case's repeats failed; the cases after it still run."""


def fail(message: str) -> NoReturn:
    log(f"run: {message}", "ERROR")
    sys.exit(1)


def check_run_id(run_id: str) -> str:
    # The runs' pods are labeled with it.
    if not LABEL_RE.fullmatch(run_id):
        raise argparse.ArgumentTypeError(
            f"{run_id!r}: use at most 63 letters, digits, '-', '_' and '.', starting and ending with a letter or digit"
        )
    return run_id


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python3 -m scenario_runner.cli.run", description=__doc__)
    p.add_argument("--scenarios-file", type=Path, default=SCENARIOS_FILE, help=f"default: {SCENARIOS_FILE}")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--scenario",
        action="append",
        metavar="NAME",
        help="a case to run, all its subcases, or one of them as <case>-<subcase>; repeat it for more",
    )
    which.add_argument(
        "--all",
        action="store_true",
        help="run every case in the scenarios file, in the order listed, each on clusters of its own",
    )
    which.add_argument(
        "--suite",
        metavar="NAME",
        help="run the cases the scenarios file's `suites` lists under this name, in the order listed",
    )
    p.add_argument(
        "--provider",
        choices=[SUBSTRATE, AGENT_SANDBOX],
        help=f"the control plane every locust case installs, with its {SANDBOX_CLASS}, in place of its `locust.{INSTALL}`'s",
    )
    p.add_argument(
        "--bare-metal",
        action=argparse.BooleanOptionalAction,
        help="run every case's sandboxed pool on bare metal, or with --no-bare-metal on VMs, whatever the case or the"
        " scenarios file's `bare-metal.default` says; noops only if they say so themselves. A case on bare metal is"
        " recorded as <case>-metal",
    )
    p.add_argument(
        "--test",
        action="append",
        metavar="NAME",
        help="an ai-sandbox-benchmark test to run, test_ prefix optional, instead of the case's"
        " `sandbox-benchmark.tests`; repeat it for more. Only for sandbox-benchmark cases",
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
        help="folder in the report bucket the reports go under, as <prefix>/<name>/<record>/. Default:"
        f" {reports.REPORT_PREFIX}",
    )
    p.add_argument(
        "--report-deployment",
        type=Path,
        metavar="DIR",
        help="an applied Terraform deployment, such as automation-infra/dev/reports, whose reports output's bucket"
        " and prefix the reports go to, in place of --report-bucket and --report-prefix",
    )
    p.add_argument(
        "--name",
        type=check_run_id,
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
        "--image-repo",
        metavar="REPO",
        help="the repository every case's cluster pulls this tree's images from,"
        " <location>-docker.pkg.dev/<project>/<name>; from a machine, the ones it lacks are built into it first."
        " Not with --dev-infra,"
        " which uses its own. Default: $IMAGE_REPO, else from a machine one applied for the execution",
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
    if not args.dev_infra and (args.no_deploy_workloads or args.dev_infra_update):
        p.error("--no-deploy-workloads and --dev-infra-update are only for --dev-infra")
    if args.dev_infra:
        if args.image_repo:
            p.error("--image-repo is not for --dev-infra, whose clusters pull from its own repository")
        # None until the dev infra is applied.
        args.image_repo = dev_infra.runner_outputs(args.dev_infra).get("docker_repo")
    else:
        args.image_repo = args.image_repo or os.environ.get("IMAGE_REPO") or None
        try:
            if args.image_repo:
                images.repository_name(args.image_repo)
        except ValueError as exc:
            p.error(f"--image-repo: {exc}")
    if args.report_deployment:
        if args.report_bucket or args.report_prefix:
            p.error("--report-deployment is instead of --report-bucket and --report-prefix")
        if not args.report_deployment.is_dir():
            p.error(f"--report-deployment: no directory {args.report_deployment}")
        try:
            out = terraform.output(args.report_deployment).get("reports")
        except (OSError, subprocess.CalledProcessError):
            p.error(f"--report-deployment: cannot read the outputs of {args.report_deployment}")
        # An unapplied deployment has no outputs.
        if not (isinstance(out, dict) and out.get("bucket") and out.get("prefix")):
            p.error(f"--report-deployment: {args.report_deployment} has no reports output with a bucket and a prefix;"
                    " is it applied?")  # fmt: skip
        args.report_bucket, args.report_prefix = out["bucket"], out["prefix"]
        dataset = f", queried in BigQuery as {out['dataset']}" if out.get("dataset") else ""
        log(f"run: reports go to gs://{args.report_bucket}/{args.report_prefix}/{dataset}")
    args.report_prefix = args.report_prefix or reports.REPORT_PREFIX
    if not re.fullmatch(r"[^/]+(/[^/]+)*", args.report_prefix):
        p.error("--report-prefix must not be empty, nor start or end with a slash")
    if args.state_backend == "gcs" and not (args.state_bucket and args.state_prefix):
        p.error("--state-backend=gcs requires --state-bucket and --state-prefix, or STATE_BUCKET and STATE_PREFIX")
    if args.state_backend == "local" and (args.state_bucket or args.state_prefix):
        p.error("--state-bucket and --state-prefix are only for --state-backend=gcs")
    return args


def selected_cases(args: argparse.Namespace) -> dict[str, dict]:
    """The cases to run, by name, in the order they run; sets args.placements,
    the bare-metal placements in --region."""
    try:
        scenarios = load_scenarios(args.scenarios_file)
        args.placements = scenarios.bare_metal.placements_in(args.region)
        return select(
            scenarios,
            None if args.all else args.scenario,
            args.suite,
            provider=args.provider,
            tests=args.test,
            bare_metal=args.bare_metal,
            placements=args.placements,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        fail(str(exc))


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
    problems = preflight.check(cases, args.project_id, args.region, applied_clusters(args, cases), args.placements)
    if problems:
        fail("cannot run, as:\n" + "\n".join(f"  - {problem}" for problem in problems))
    if not args.dev_infra and not args.image_repo and any(map(pulls_images, cases.values())):
        if ON_CLOUD_RUN:
            fail("--image-repo, or IMAGE_REPO, is required: the repository the images were built into")
        log("run: no --image-repo, so the execution applies a repository of its own, builds the images into it and"
            " destroys it after its clusters")  # fmt: skip
    # A dev infra not applied yet, or the execution's own repository, has none
    # to check until it is.
    if args.image_repo:
        check_images(args, cases)
    if args.report_bucket or all(case["type"] == noop.TYPE for case in cases.values()):
        return
    if args.dev_infra:
        log(
            f"run: no --report-bucket, so the reports go to dev infra {args.dev_infra}'s cluster bucket, which goes"
            f" when the dev infra is destroyed, and are downloaded to {reports.REPORTS_DIR / args.name}"
        )
        return
    log(
        "run: no --report-bucket, so each case's reports go to its cluster's own bucket, which is destroyed with"
        f" it; the only copy that lasts is the one downloaded to {reports.REPORTS_DIR / args.name}",
        "WARNING",
    )


def pulls_images(case: dict) -> bool:
    """Whether the case's cluster pulls from the image repository."""
    return control_plane(case) is not None or bool(benchmarks.image_files(case))


def needed_images(args: argparse.Namespace, cases: dict[str, dict]) -> dict[str, list[str]]:
    """The images the cases pull from args.image_repo, by builder.py's group."""
    needed = {}
    if any(control_plane(case) == SUBSTRATE for case in cases.values()):
        needed["substrate"] = substrate.image_refs(args.image_repo)
    if any(control_plane(case) == AGENT_SANDBOX for case in cases.values()):
        needed["agent-sandbox"] = agent_sandbox.image_refs(args.image_repo)
    # env's images are only read when it is deployed, for the substrate provider.
    if not args.no_deploy_workloads and any(
        sandbox_benchmark.provider_name(case) == "substrate"
        for case in cases.values()
        if case["type"] == sandbox_benchmark.TYPE
    ):
        needed["env"] = ate_env.image_refs(args.image_repo)
    files = list(dict.fromkeys(file for case in cases.values() for file in benchmarks.image_files(case)))
    if files:
        needed["benchmark"] = images.refs(args.image_repo, files)
    return needed


def check_images(args: argparse.Namespace, cases: dict[str, dict]) -> None:
    """Fails unless args.image_repo has every image the cases pull, naming
    the ones it lacks and the build-images that builds them; from a machine
    rather than Cloud Run, builds them instead."""
    needed = needed_images(args, cases)
    log(f"run: checking {args.image_repo} has the images of this tree that {', '.join(cases)} pull")
    absent = {group: images.missing(refs) for group, refs in needed.items()}
    absent = {group: refs for group, refs in absent.items() if refs}
    if absent and not ON_CLOUD_RUN:
        build_images(args, absent)
    elif absent:
        where = f"--dev-infra={args.dev_infra}" if args.dev_infra else f"--repo={args.image_repo}"
        fail(
            f"{args.image_repo} lacks these images of this tree, or gcloud's account cannot read them:\n"
            + "\n".join(f"  {ref}" for refs in absent.values() for ref in refs)
            + f"\nBuild them with: benchmarking/tools/build-images {where} --images={','.join(absent)}"
        )
    for group in needed:
        if builder.dirty(group):
            log(
                f"run: {group}'s images are of a -dirty version, which build-images builds again every time but"
                " which does not say which uncommitted changes they are of; build them after the last change",
                "WARNING",
            )


def build_images(args: argparse.Namespace, absent: dict[str, list[str]]) -> None:
    """Builds the images args.image_repo lacks, by builder.py's group, as
    build-images does."""
    missing = builder.missing_requirements(list(absent))
    if missing:
        fail(f"building the images {args.image_repo} lacks requires {', '.join(missing)}")
    try:
        if "substrate" in absent:
            substrate.check_pinned()
        for group, refs in absent.items():
            log(f"run: {group}: building\n" + "\n".join(f"  {ref}" for ref in refs))
            builder.build(group, args.image_repo, refs, login=True)
    except subprocess.CalledProcessError as exc:
        fail(failure(exc))
    except (images.ImageError, substrate.SubstrateError) as exc:
        fail(str(exc))


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
    wanted = benchmarks.cluster_config(first, cases[first])
    if record is not None:
        if (record["project_id"], record["region"]) != (args.project_id, args.region):
            fail(
                f"dev infra {args.dev_infra} is in project {record['project_id']} and region {record['region']},"
                f" not {args.project_id} and {args.region}"
            )
        # Another config under the same state would replace every resource.
        if args.dev_infra_update and wanted.name != record["cluster"]["name"]:
            fail(
                f"case {first} runs on infra/clusters/{wanted.name}, but dev infra {args.dev_infra} is"
                f" infra/clusters/{record['cluster']['name']}; destroy it rather than update it"
            )
        if not args.dev_infra_update:
            try:
                from_record(record["cluster"])
            except ValueError:
                fail(
                    f"dev infra {args.dev_infra} records its cluster as it was asked for before cluster configs;"
                    " run with --dev-infra-update to apply it with the first case's cluster, or destroy it with"
                    f" python3 -m scenario_runner.cli.dev_infra destroy {args.dev_infra}"
                )
    if record is None or args.dev_infra_update:
        record = {
            **(record or {}),
            "name": args.dev_infra,
            "test_case": first,
            "project_id": args.project_id,
            "region": args.region,
            "cluster": to_record(wanted),
            "bare_metal": cases[first][BARE_METAL],
            # The ones to try if the cluster's zone is out of its machine type.
            "placements": args.placements if cases[first][BARE_METAL] else [],
            dev_infra.SKIP_SUBSTRATE_INSTALL: control_plane(cases[first]) != SUBSTRATE,
        }
    current = from_record(record["cluster"])
    for name, case in cases.items():
        config = benchmarks.cluster_config(name, case)
        # A configuration's subclass is its cluster and more, such as openclaw's common's.
        if not isinstance(current, type(config)):
            fail(
                f"case {name} runs on configuration {config.CONFIGURATION}, but dev infra {args.dev_infra}"
                f" is {current.CONFIGURATION}"
            )
        if sandbox_class(case) == "microvm" and not current.sandboxed_kvm and not case[BARE_METAL]:
            fail(
                f"case {name} is microvm, but dev infra {args.dev_infra}'s sandboxed pool has no /dev/kvm for its"
                " guests; use a dev infra of its own"
            )
        # Its records say which it ran on.
        if case[BARE_METAL] != record.get("bare_metal", False):
            fail(
                f"case {name} is {'' if case[BARE_METAL] else 'not '}on bare metal, but dev infra {args.dev_infra}'s"
                f" sandboxed pool is{'' if record.get('bare_metal') else ' not'}; use a dev infra of its own"
            )
        # A bare-metal one is where it was applied.
        asked, have = config, current
        if case[BARE_METAL] and record.get("placement"):
            asked, have = asked.placed(record["placement"]), have.placed(record["placement"])
        if asked != have:
            log(f"run: case {name}'s cluster differs from dev infra {args.dev_infra}'s it runs on", "WARNING")
    json_output = args.terraform_output == "json"
    if args.dev_infra_update:
        log(f"run: updating dev infra {args.dev_infra} from the source")
        record = dev_infra.apply(record, json_output, update=True)
    elif not record.get("complete"):
        log(f"run: applying dev infra {args.dev_infra}, which is not complete")
        record = dev_infra.apply(record, json_output)
    return record


def run_case(
    args: argparse.Namespace,
    record: str,
    case: dict,
    cluster: Cluster,
    name: str,
    repeat: int,
    warmup: int,
    image_repo: str,
) -> None:
    benchmarks.run(
        record,
        case,
        args.name,
        cluster,
        image_repo=image_repo,
        deploy_workloads=not args.no_deploy_workloads,
        test_case=name,
        repeat=repeat,
        warmup=warmup,
        report_bucket=args.report_bucket,
        report_prefix=args.report_prefix,
        keeps_bucket=bool(args.dev_infra),
    )


def describe(exc: BaseException) -> str:
    return failure(exc) if isinstance(exc, subprocess.CalledProcessError) else str(exc)


def run_repeats(name: str, case: dict, run: Callable[[str, int, int], None], shared_cluster: bool) -> None:
    """Calls run with each of the case's warm-ups and then its repeats: the
    record name, the repeat number, 0 for a warm-up or a case run once, and the
    warm-up number, 0 for a repeat. A failed repeat does not stop the ones after
    it; the case fails once they have run. A failed warm-up only warns. A
    cluster that could not be destroyed stops them, and on a shared_cluster a
    run that may still be going, as run_cases says."""
    warmups = warmup_names(name, case)
    for n, record in enumerate(warmups, 1):
        log(f"{name}: warm-up {n} of {len(warmups)}, as {record}")
        try:
            run(record, 0, n)
        except RUN_ERRORS as exc:
            if shared_cluster and isinstance(exc, benchmarks.STILL_RUNNING):
                raise
            log(f"{record}: {describe(exc)}; only a warm-up, so {name} goes on", "WARNING")
    records = record_names(name, case)
    if len(records) == 1:
        run(name, 0, 0)
        return
    failed = []
    for n, record in enumerate(records, 1):
        log(f"{name}: repeat {n} of {len(records)}, as {record}")
        try:
            run(record, n, 0)
        except benchmarks.STILL_RUNNING as exc:
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
    args: argparse.Namespace,
    cases: dict[str, dict],
    run: Callable[[str, dict, Cluster | None], None],
    shared_cluster: bool,
    applied: Callable[[str, dict], AbstractContextManager[Cluster | None]] = lambda key, case: nullcontext(),
    summary_bucket: str | None = None,
) -> list[str]:
    """Calls run with each case and the cluster applied gives each batch of
    them, recorded as its key, and returns the ones that failed. A case's
    subcases run one after another, then its type's summarize, given
    summary_bucket. A failed case does not stop the ones after it; an
    interrupt, or a cluster that could not be destroyed, does. So does a run
    that may still be going on a shared_cluster, which the next case would
    deploy under it; on a batch's cluster, which is destroyed after it, the
    rest of the batch does not run, and the next batch does."""
    failed = []
    for group, members in grouped(cases).items():
        for batch in batches(members, cases):
            todo = list(batch)
            try:
                with applied(batch_key(group, members, batch), cases[batch[0]]) as cluster:
                    while todo:
                        name = todo.pop(0)
                        case = cases[name]
                        try:
                            run(name, case, cluster)
                        except benchmarks.STILL_RUNNING as exc:
                            if shared_cluster:
                                raise
                            log(f"{name}: {exc}", "ERROR")
                            failed.append(name)
                            if todo:
                                log(f"{', '.join(todo)}: not run, on a cluster {name} may still be running on", "ERROR")
                                failed += todo
                                todo = []
                        except (CaseFailed, *RUN_ERRORS) as exc:
                            log(f"{name}: {describe(exc)}", "ERROR")
                            failed.append(name)
                        # From what was downloaded, so not on Cloud Run.
                        records = record_names(name, case)
                        if len(records) > 1 and case["type"] in benchmarks.SUMMARIZED and not ON_CLOUD_RUN:
                            summary.write(reports.REPORTS_DIR / args.name, name, records)
            except benchmarks.STILL_RUNNING:
                raise
            except RUN_ERRORS as exc:
                # The batch's cluster: its apply, install or cleanup.
                log(f"{', '.join(todo or batch)}: {describe(exc)}", "ERROR")
                failed += [name for name in todo or batch if name not in failed]
        runs = [(record, name, cases[name]) for name in members for record in run_names(name, cases[name])]
        benchmarks.summarize(runs, args.name, summary_bucket, args.report_prefix)
    return failed


def run_on_dev_infra(args: argparse.Namespace, cases: dict[str, dict]) -> list[str]:
    """A case's repeats run one after another on the dev infra's cluster.
    Returns the cases that failed."""
    checked = bool(args.image_repo)
    record = prepare_dev_infra(args, cases)
    cluster = dev_infra.cluster(args.dev_infra)
    image_repo = args.image_repo = dev_infra.runner_outputs(args.dev_infra)["docker_repo"]
    if not checked:
        check_images(args, cases)

    def run(name: str, case: dict, _: Cluster | None) -> None:
        nonlocal record
        if control_plane(case) == AGENT_SANDBOX:
            record = dev_infra.install_agent_sandbox(record)
        elif control_plane(case) == SUBSTRATE:
            record = dev_infra.install_substrate(record, sandbox_class(case) == "microvm")
        run_repeats(
            name,
            case,
            lambda record, repeat, warmup: run_case(args, record, case, cluster, name, repeat, warmup, image_repo),
            shared_cluster=True,
        )

    return run_cases(args, cases, run, shared_cluster=True, applied=lambda key, case: nullcontext(cluster),
                     summary_bucket=args.report_bucket or cluster.bucket)  # fmt: skip


def on_placements(
    record: str, placements: list, attempt: Callable[[dict | None, bool], None], start: int = 0
) -> int:
    """Calls attempt with each of placements in turn from start, wrapping
    around, and whether it is the last, until one does not end in a stockout,
    which only the cluster's apply raises: bare metal runs out by zone, so the
    cluster, destroyed first, moves to the next. Returns the index of the one
    that did not; the last one's stockout is raised."""
    order = [*range(start, len(placements)), *range(start)]
    for n, index in enumerate(order, 1):
        placement = placements[index]
        try:
            attempt(placement, n == len(order))
            return index
        except terraform.Stockout:
            if n == len(order):
                raise
            log(f"{record}: {placement['zone']} is out of {placement['machine-type']}; trying the next placement,"
                f" {n + 1} of {len(order)}", "WARNING")  # fmt: skip


def run_on_own_infra(args: argparse.Namespace, cases: dict[str, dict]) -> list[str]:
    """A case's warm-ups and repeats run on a cluster applied for it and
    destroyed after them, or with `repeats.cluster: per-repeat` each repeat on
    one of its own; a batch of a case's subcases on one applied for them all.
    The clusters pull from args.image_repo, or without one from
    a repository applied for the execution, the images built into it, and
    destroyed after the clusters. Returns the cases that failed."""
    # Every cluster's, before anything is applied, so a name that can't be fails first.
    case_vars = {key: names.infra_vars(args.project_id, args.region, args.name, key) for key in cluster_keys(cases)}
    # The placement the last bare-metal cluster was applied at, which the next
    # starts from, as a zone stays out of a machine type for hours.
    current_placement = 0

    @contextmanager
    def applied_cluster(case: dict, key: str) -> Iterator[Cluster]:
        """The case's cluster, applied as scenarios/<key> with what the case
        runs on installed, and destroyed after. A bare-metal case's goes on its
        placements in turn, from the last one applied, until one has the
        machine type."""
        nonlocal current_placement
        config = benchmarks.cluster_config(key, case)
        version = substrate.build_version() if control_plane(case) == SUBSTRATE else None
        uncreated = deployment(args, "scenarios", key)
        # A cluster that pulls none of the images is not granted the repository.
        pulls = pulls_images(case)
        repository = images.repository_name(args.image_repo) if pulls else None
        applied = None

        def attempt(placement: dict | None, last: bool) -> None:
            nonlocal applied
            spec = config.placed(placement) if placement else config
            case_infra = spec.deployment(uncreated, execution(case_vars[key], repository, version))
            stockout = None
            if placement:
                log(f"{key}: sandboxed pool on {placement['machine-type']} in {placement['zone']}")
                cluster_name = case_vars[key]["default_resource_name"]
                stockout = partial(gke_stockout, args.project_id, placement["zone"], cluster_name, utc_now())
            stack.enter_context(case_infra.applied(retry_stockout=last, stockout=stockout))
            applied = case_infra

        with ExitStack() as stack:
            if case[BARE_METAL]:
                current_placement = on_placements(key, args.placements, attempt, current_placement)
            else:
                attempt(None, True)
            cluster = Cluster(applied.outputs(), applied.data / "kubeconfig")
            try:
                if control_plane(case) == AGENT_SANDBOX:
                    agent_sandbox.install(cluster)
                elif control_plane(case) == SUBSTRATE:
                    substrate.install(cluster, sandbox_class(case) == "microvm")
                yield cluster
            finally:
                if pulls:
                    cluster.delete_volumes()

    @contextmanager
    def batch_cluster(key: str, case: dict) -> Iterator[Cluster | None]:
        """The cluster of a batch whose repeats share one; None for one whose
        repeats have their own."""
        if not shares_cluster(case):
            yield None
            return
        with applied_cluster(case, key) as cluster:
            yield cluster

    def run_on_clusters(name: str, case: dict, cluster: Cluster | None) -> None:
        if cluster is not None:
            run_repeats(
                name,
                case,
                lambda record, repeat, warmup: run_case(
                    args, record, case, cluster, name, repeat, warmup, args.image_repo
                ),
                shared_cluster=True,
            )
            return

        def run_on_cluster(record: str, repeat: int, warmup: int) -> None:
            with applied_cluster(case, record) as cluster:
                run_case(args, record, case, cluster, name, repeat, warmup, args.image_repo)

        run_repeats(name, case, run_on_cluster, shared_cluster=False)

    def run_all() -> list[str]:
        return run_cases(args, cases, run_on_clusters, shared_cluster=False, applied=batch_cluster,
                         summary_bucket=args.report_bucket)  # fmt: skip

    if args.image_repo or not any(map(pulls_images, cases.values())):
        return run_all()
    variables = names.infra_vars(args.project_id, args.region, args.name)
    runner_infra = deployment(args, "scenario-runner").create(RUNNER_INFRA_DIR, variables)
    with runner_infra.applied():
        args.image_repo = runner_infra.outputs()["docker_repo"]
        check_images(args, cases)
        return run_all()


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
