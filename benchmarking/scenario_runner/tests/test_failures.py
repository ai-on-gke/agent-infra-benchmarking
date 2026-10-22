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

"""How the scenario-runner fails: what stops a series, what is reported, and
what is cleaned up."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scenario_runner import deployment, jobs, locust, process, terraform
from scenario_runner.cli import run
from scenario_runner import cluster as cluster_module
from scenario_runner.cluster import Cluster

# A locust case as far as the series reads it: its type, and the cluster its
# mapping asks for.
LOCUST = {"type": "locust", "locust": {}}


class Environ(unittest.TestCase):
    def test_none_removes_a_variable(self):
        with mock.patch.dict(os.environ, {"KATA_VER": "9.9", "KEEP": "1"}):
            env = process.environ({"KATA_VER": None, "ADDED": "2"})
        self.assertNotIn("KATA_VER", env)
        self.assertEqual((env["KEEP"], env["ADDED"]), ("1", "2"))

    def test_nothing_to_add_inherits(self):
        self.assertIsNone(process.environ(None))


class Shield(unittest.TestCase):
    def interrupt(self) -> None:
        os.kill(os.getpid(), signal.SIGINT)
        # The handler runs at the next bytecode boundary of the main thread.
        time.sleep(0.01)

    def test_a_single_ctrl_c_only_warns_and_a_second_interrupts(self):
        with mock.patch.object(process, "log") as log, self.assertRaises(KeyboardInterrupt):
            with process.shielded("the destroy"):
                self.interrupt()
                self.assertIn("the destroy goes on", log.call_args.args[0])
                self.interrupt()
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_commands_run_out_of_the_terminals_reach(self):
        with process.shielded("the destroy"):
            pgid = process.output(["sh", "-c", "ps -o pgid= -p $$"]).strip()
        self.assertNotEqual(int(pgid), os.getpgid(0))


class Destroy(unittest.TestCase):
    def deployment(self, destroy_error: BaseException | None) -> deployment.Deployment:
        dep = deployment.Deployment(Path(tempfile.mkdtemp()))

        def deploy(*args, **kwargs):
            if "-destroy" in args:
                self.assertTrue(process._shielding, "the cleanup destroy has to be shielded")
                if destroy_error:
                    raise destroy_error

        patcher = mock.patch.multiple(dep.__class__, init=mock.DEFAULT, deploy=mock.DEFAULT)
        mocks = patcher.start()
        self.addCleanup(patcher.stop)
        mocks["deploy"].side_effect = lambda *args, **kwargs: deploy(*args, **kwargs)
        return dep

    def test_a_failed_destroy_after_a_failure_names_both(self):
        dep = self.deployment(subprocess.CalledProcessError(1, ["terraform"]))
        with mock.patch.object(deployment, "log") as log, \
             self.assertRaises(deployment.DestroyFailed) as caught:
            with dep.applied():
                raise RuntimeError("the run failed")
        self.assertIn("the run failed", str(caught.exception))
        self.assertIn("the run failed", log.call_args.args[0])

    def test_a_failed_destroy_after_success_raises(self):
        dep = self.deployment(subprocess.CalledProcessError(1, ["terraform"]))
        with self.assertRaises(deployment.DestroyFailed):
            with dep.applied():
                pass

    def test_an_interrupted_destroy_stays_an_interrupt(self):
        dep = self.deployment(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            with dep.applied():
                raise RuntimeError("the run failed")

    def test_the_original_error_goes_on_after_a_good_destroy(self):
        dep = self.deployment(None)
        with self.assertRaisesRegex(RuntimeError, "the run failed"):
            with dep.applied():
                raise RuntimeError("the run failed")


class Stockouts(unittest.TestCase):
    def test_a_failure_that_tells_of_a_stockout_is_one(self):
        message = "Error: googleapi: Error 400: The zone us-central1-a does not have enough resources available"
        with self.assertRaises(terraform.Stockout):
            terraform.watch_stockout(["sh", "-c", f"echo '{message}'; exit 1"], lambda line: None, None)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            terraform.watch_stockout(["sh", "-c", "echo 'Error: quota'; exit 1"], lambda line: None, None)
        self.assertNotIsInstance(caught.exception, terraform.Stockout)

    def test_a_stockout_is_retried_unless_given_up_on(self):
        calls = []

        def apply():
            calls.append(1)
            raise terraform.Stockout(1, ["terraform", "-chdir=x", "apply"])

        with mock.patch.object(process, "log"), mock.patch.object(process.time, "sleep"):
            with self.assertRaises(terraform.Stockout):
                process.retry("x", apply, give_up=(terraform.Stockout,))
            self.assertEqual(len(calls), 1)
            with self.assertRaises(terraform.Stockout):
                process.retry("x", apply)
        self.assertEqual(len(calls), 1 + process.ATTEMPTS)

    def test_a_stockout_moves_on_to_the_next_placement(self):
        placements = [{"zone": z, "machine-type": "c3-standard-192-metal"} for z in ("a", "b", "c")]
        tried = []

        def attempt(placement, last):
            tried.append((placement["zone"], last))
            if placement["zone"] != "c":
                raise terraform.Stockout(1, ["terraform", "-chdir=x", "apply"])

        with mock.patch.object(run, "log"):
            self.assertEqual(run.on_placements("r", placements, attempt), 2)
        self.assertEqual(tried, [("a", False), ("b", False), ("c", True)])

    def test_placements_are_tried_from_the_one_that_last_worked_around(self):
        placements = [{"zone": z, "machine-type": "m"} for z in ("a", "b", "c")]
        tried = []

        def attempt(placement, last):
            tried.append(placement["zone"])
            if placement["zone"] != "a":
                raise terraform.Stockout(1, ["terraform", "-chdir=x", "apply"])

        with mock.patch.object(run, "log"):
            self.assertEqual(run.on_placements("r", placements, attempt, start=1), 0)
        self.assertEqual(tried, ["b", "c", "a"])

    def test_a_failure_gke_tells_as_a_stockout_is_one(self):
        dep = deployment.Deployment(Path(tempfile.mkdtemp()))
        failed = subprocess.CalledProcessError(1, ["terraform", "-chdir=x", "apply"])
        with mock.patch.object(deployment.terraform, "deploy", side_effect=failed):
            with self.assertRaises(terraform.Stockout):
                dep.deploy(retry_stockout=False, stockout=lambda: True)
            with mock.patch.object(process, "log"), mock.patch.object(process.time, "sleep"), \
                 self.assertRaises(subprocess.CalledProcessError) as caught:
                dep.deploy(stockout=lambda: False)
        self.assertNotIsInstance(caught.exception, terraform.Stockout)

    def test_the_last_placements_stockout_and_any_other_failure_are_raised(self):
        def stockout(placement, last):
            raise terraform.Stockout(1, ["terraform", "-chdir=x", "apply"])

        def failure(placement, last):
            raise locust.LocustError("boom")

        placements = [{"zone": "a", "machine-type": "m"}, {"zone": "b", "machine-type": "m"}]
        with mock.patch.object(run, "log"):
            with self.assertRaises(terraform.Stockout):
                run.on_placements("r", placements, stockout)
            with self.assertRaises(locust.LocustError):
                run.on_placements("r", placements, failure)
        run.on_placements("r", [None], lambda placement, last: self.assertTrue(last))


class GkeStockout(unittest.TestCase):
    OPERATIONS = [
        {"operationType": "CREATE_NODE_POOL", "startTime": "2026-10-21T13:03:59.2Z",
         "statusMessage": "Not all instances running in IGM. Current errors: [GCE_STOCKOUT]: ..."},
        {"operationType": "CREATE_CLUSTER", "startTime": "2026-10-21T12:51:30.9Z", "statusMessage": ""},
    ]  # fmt: skip

    def stockout(self, since: str, operations=OPERATIONS, returncode=0) -> bool:
        import json

        proc = subprocess.CompletedProcess([], returncode, stdout=json.dumps(operations), stderr="denied")
        with mock.patch.object(cluster_module.subprocess, "run", return_value=proc), \
             mock.patch.object(cluster_module, "log"):
            return cluster_module.gke_stockout("p", "us-central1-c", "c", since)

    def test_a_create_that_failed_on_a_stockout(self):
        self.assertTrue(self.stockout("2026-10-21T13:00:00Z"))

    def test_one_from_before_the_apply_is_not_held_against_it(self):
        self.assertFalse(self.stockout("2026-10-21T13:10:00Z"))

    def test_other_failures_and_unreadable_operations_are_not_stockouts(self):
        quota = [{**self.OPERATIONS[0], "statusMessage": "Quota 'C3_CPUS' exceeded"}]
        self.assertFalse(self.stockout("2026-10-21T13:00:00Z", quota))
        self.assertFalse(self.stockout("2026-10-21T13:00:00Z", returncode=1))


class Series(unittest.TestCase):
    args = mock.Mock(name="args")

    def setUp(self):
        patcher = mock.patch.object(run, "summary")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_repeat_does_not_stop_the_others(self):
        ran = []

        def repeat(record, n, warmup):
            ran.append(record)
            if n == 1:
                raise locust.LocustError("boom")

        with self.assertRaises(run.CaseFailed):
            run.run_repeats("c", {"repeats": {"count": 3, "cluster": "per-repeat"}}, repeat, shared_cluster=False)
        self.assertEqual(ran, ["c-1", "c-2", "c-3"])

    def test_a_failed_warmup_only_warns(self):
        ran = []

        def repeat(record, n, warmup):
            ran.append((record, n, warmup))
            if warmup:
                raise locust.LocustError("cold")

        case = {"repeats": {"count": 2, "warmups": 2}}
        with mock.patch.object(run, "log") as log:
            run.run_repeats("c", case, repeat, shared_cluster=True)
        self.assertEqual(ran, [("c-warmup-1", 0, 1), ("c-warmup-2", 0, 2), ("c-1", 1, 0), ("c-2", 2, 0)])
        self.assertTrue(any("only a warm-up" in call.args[0] for call in log.call_args_list))

    def test_a_stuck_run_stops_a_shared_cluster_only(self):
        def stuck(record, n, warmup):
            raise locust.LocustTimeout("still going")

        with self.assertRaises(locust.LocustStillRunning):
            run.run_repeats("c", {"repeats": {"count": 2, "warmups": 0}}, stuck, shared_cluster=True)
        with self.assertRaises(locust.LocustStillRunning):
            run.run_repeats("c", {}, stuck, shared_cluster=True)
        with self.assertRaises(run.CaseFailed):
            per_repeat = {"repeats": {"count": 2, "cluster": "per-repeat"}}
            run.run_repeats("c", per_repeat, stuck, shared_cluster=False)

    def test_a_failed_case_does_not_stop_the_series(self):
        cases = {"a": LOCUST, "b": LOCUST}
        ran = []

        def case(name, _case, _cluster):
            ran.append(name)
            if name == "a":
                raise locust.LocustStillRunning("unread")

        self.assertEqual(run.run_cases(self.args, cases, case, shared_cluster=False), ["a"])
        self.assertEqual(ran, ["a", "b"])
        with self.assertRaises(locust.LocustStillRunning):
            run.run_cases(self.args, cases, case, shared_cluster=True)

    def variants(self, *names: str, infra: dict | None = None) -> dict:
        from scenario_runner.reports import VARIANT

        return {f"g-{n}": {**LOCUST, "locust": {"infra": infra}, VARIANT: {"of": "g", "name": n}} for n in names}

    def test_variants_alike_share_a_cluster_then_are_summarized(self):
        from contextlib import contextmanager

        from scenario_runner import benchmark

        other = {"cluster": {"sandboxed-nodes": 2}}
        cases = {**self.variants("a", "b"), **self.variants("c", infra=other), "h": LOCUST}
        cases = {name: cases[name] for name in ["g-a", "g-c", "g-b", "h"]}
        applied, ran = [], []

        @contextmanager
        def cluster(key, case):
            applied.append(key)
            yield key

        with mock.patch.object(benchmark, "summarize") as summarize:
            failed = run.run_cases(self.args, cases, lambda name, case, cluster: ran.append((name, cluster)),
                                   shared_cluster=False, applied=cluster)  # fmt: skip
        self.assertEqual(failed, [])
        self.assertEqual(applied, ["g-a", "g-c", "h"])
        self.assertEqual(ran, [("g-a", "g-a"), ("g-b", "g-a"), ("g-c", "g-c"), ("h", "h")])
        self.assertEqual(run.cluster_keys(cases), ["g-a", "g-c", "h"])
        groups = [[record for record, _, _ in call.args[0]] for call in summarize.call_args_list]
        self.assertEqual(groups, [["g-a-warmup-1", "g-a", "g-c-warmup-1", "g-c", "g-b-warmup-1", "g-b"], ["h-warmup-1", "h"]])

    def test_all_of_a_group_on_one_cluster_is_recorded_as_the_group(self):
        self.assertEqual(run.cluster_keys(self.variants("a", "b")), ["g"])

    def test_a_stuck_variant_leaves_the_rest_of_its_batch_unrun(self):
        cases = self.variants("a", "b", "c")
        ran = []

        def case(name, _case, _cluster):
            ran.append(name)
            if name == "g-a":
                raise locust.LocustStillRunning("unread")

        self.assertEqual(run.run_cases(self.args, cases, case, shared_cluster=False), ["g-a", "g-b", "g-c"])
        self.assertEqual(ran, ["g-a"])

    def test_a_failed_destroy_stops_the_series(self):
        def case(name, _case, _cluster):
            raise deployment.DestroyFailed(Path("/x"), None)

        with self.assertRaises(deployment.DestroyFailed):
            run.run_cases(self.args, {"a": LOCUST, "b": LOCUST}, case, shared_cluster=False)


class Args(unittest.TestCase):
    REPO = "--image-repo=us-central1-docker.pkg.dev/p/r"

    def parse(self, *argv: str, repo: str | None = REPO):
        flags = ["--project-id=p", "--state-backend=local", *([repo] if repo else [])]
        with mock.patch.object(sys, "argv", ["run", *flags, *argv]), mock.patch.dict(os.environ, {"IMAGE_REPO": ""}):
            return run.parse_args()

    def test_cases_have_to_be_named_or_all(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.parse()
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.parse("--all", "--scenario=glutton")
        self.assertTrue(self.parse("--all").all)
        self.assertEqual(self.parse("--scenario=glutton").scenario, ["glutton"])

    def test_the_image_repository_is_checked(self):
        self.assertIsNone(self.parse("--all", repo=None).image_repo)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.parse("--all", repo="--image-repo=gcr.io/p/r")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.parse("--all", "--dev-infra=d")


class Jobs(unittest.TestCase):
    cluster = Cluster({"cluster_name": "c", "region": "r", "project_id": "p", "bucket_name": "b"}, Path("/kubeconfig"))

    def status(self, stdout: str, returncode: int = 0) -> mock.Mock:
        return mock.Mock(returncode=returncode, stdout=stdout, stderr="boom")

    def test_job_status(self):
        with mock.patch.object(subprocess, "run", return_value=self.status("")):
            self.assertIsNone(jobs.job_status(self.cluster, "j", "ns"))
        with mock.patch.object(subprocess, "run", return_value=self.status("SuccessCriteriaMet Complete")):
            self.assertEqual(jobs.job_status(self.cluster, "j", "ns"), "Complete")
        with mock.patch.object(subprocess, "run", return_value=self.status("FailureTarget Failed")):
            self.assertEqual(jobs.job_status(self.cluster, "j", "ns"), "Failed")
        with mock.patch.object(subprocess, "run", return_value=self.status("", 1)), \
             self.assertRaises(jobs.StatusUnread):
            jobs.job_status(self.cluster, "j", "ns")

    def test_unreadable_jobs_are_given_up_on_within_a_minute(self):
        clock = iter(range(0, 10_000, 5))
        with mock.patch.object(jobs, "job_status", side_effect=jobs.StatusUnread("gone")), \
             mock.patch.object(locust.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(locust.time, "sleep"), mock.patch.object(locust, "log"), \
             self.assertRaises(locust.LocustStillRunning):
            locust.wait_for_jobs(self.cluster, lambda grace: None)

    def test_a_failed_job_fails_the_run(self):
        statuses = {locust.MASTER_JOB: "Complete", locust.WORKER_JOB: "Failed"}
        with mock.patch.object(jobs, "job_status", side_effect=lambda cluster, job, namespace: statuses[job]), \
             self.assertRaisesRegex(locust.LocustError, "boomer-worker failed.*--kubeconfig=/kubeconfig"):
            locust.wait_for_jobs(self.cluster, lambda grace: None)


class NeededImages(unittest.TestCase):
    """What the runner checks its repository has, by the cases' types."""

    def needed(self, cases: dict) -> dict:
        import argparse

        from scenario_runner import agent_sandbox, ate_env, images, sandbox_benchmark, substrate

        args = argparse.Namespace(no_deploy_workloads=False, image_repo="repo")
        with mock.patch.object(images, "tag", return_value="t"), \
             mock.patch.object(substrate, "build_version", return_value="v"), \
             mock.patch.object(agent_sandbox, "build_version", return_value="s"), \
             mock.patch.object(ate_env, "build_version", return_value="e"), \
             mock.patch.object(sandbox_benchmark, "test_names", return_value=None):  # fmt: skip
            return run.needed_images(args, cases)

    def test_locust_needs_its_the_workloads_and_substrate(self):
        from scenario_runner import images

        needed = self.needed({"a": {"type": "locust"}, "b": {"type": "noop"}})
        self.assertEqual(set(needed), {"substrate", "benchmark"})
        with mock.patch.object(images, "tag", return_value="t"):
            self.assertEqual(needed["benchmark"], images.refs("repo", [images.LOCUST_IMAGES, images.WORKLOADS_IMAGES]))
        self.assertIn("repo/substrate/atelet:v", needed["substrate"])
        self.assertIn("repo/substrate/glutton:v", needed["substrate"])
        self.assertIn("repo/substrate/envoy-dataplane:v", needed["substrate"])

    def test_sandbox_benchmark_needs_env_only_for_substrate(self):
        options = {"tests": ["test_x"], "runs": 1}
        case = {"type": "sandbox-benchmark", "sandbox-benchmark": options}
        needed = self.needed({"a": case})
        self.assertEqual(set(needed), {"substrate", "env", "benchmark"})
        self.assertEqual(needed["env"], ["repo/env/ate-env-api:e", "repo/env/ate-env-guest:e"])
        cloud = {**case, "control-plane": "none", "sandbox-benchmark": {**options, "provider": {"name": "e2b"}}}
        self.assertEqual(set(self.needed({"a": cloud})), {"benchmark"})

    def test_noop_needs_only_what_its_install_does(self):
        self.assertEqual(set(self.needed({"a": {"type": "noop"}})), {"substrate"})
        self.assertEqual(set(self.needed({"a": {"type": "noop", "control-plane": "agent-sandbox"}})), {"agent-sandbox"})
        self.assertEqual(self.needed({"a": {"type": "noop", "control-plane": "none"}}), {})


class BenchmarkRun(unittest.TestCase):
    def test_options_reach_the_type_s_run_as_the_runner_passes_them(self):
        from scenario_runner import benchmark, monitoring

        cluster = mock.Mock()
        with mock.patch.object(monitoring, "deploy"), mock.patch.object(locust, "run") as run:
            benchmark.run("c-1", {"type": "locust"}, "run-1", cluster, image_repo="repo", test_case="c", repeat=1)
        run.assert_called_once_with(cluster, "c-1", {"type": "locust"}, "run-1", image_repo="repo", test_case="c", repeat=1)

    def test_locust_run_takes_them_by_name(self):
        import inspect

        params = inspect.signature(locust.run).parameters
        self.assertEqual(list(params)[:4], ["cluster", "name", "case", "run_id"])
        self.assertEqual(params["image_repo"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_every_type_takes_the_options_the_runner_passes(self):
        import argparse
        import inspect

        from scenario_runner import benchmark

        args = argparse.Namespace(name="run-1", no_deploy_workloads=False, report_bucket=None,
                                  report_prefix="reports", dev_infra=None)  # fmt: skip
        with mock.patch.object(benchmark, "run") as bench_run:
            run.run_case(args, "c-1", {"type": "locust"}, mock.Mock(), "c", 1, 0, "repo")
        for type_, module in benchmark.RUNNERS.items():
            params = inspect.signature(module.run).parameters
            self.assertLessEqual(set(bench_run.call_args.kwargs), set(params), type_)
