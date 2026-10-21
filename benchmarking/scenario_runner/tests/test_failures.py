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

from scenario_runner import deployment, jobs, locust, process
from scenario_runner.cli import run
from scenario_runner.cluster import Cluster


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

        def deploy(*args):
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


class Series(unittest.TestCase):
    args = mock.Mock(name="args")

    def setUp(self):
        patcher = mock.patch.object(run, "summary")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_repeat_does_not_stop_the_others(self):
        ran = []

        def repeat(record, n):
            ran.append(record)
            if n == 1:
                raise locust.LocustError("boom")

        with self.assertRaises(run.CaseFailed):
            run.run_repeats("c", {"repeats": 3}, repeat, shared_cluster=False)
        self.assertEqual(ran, ["c-1", "c-2", "c-3"])

    def test_a_stuck_run_stops_a_shared_cluster_only(self):
        def stuck(record, n):
            raise locust.LocustTimeout("still going")

        with self.assertRaises(locust.LocustStillRunning):
            run.run_repeats("c", {"repeats": 2}, stuck, shared_cluster=True)
        with self.assertRaises(run.CaseFailed):
            run.run_repeats("c", {"repeats": 2}, stuck, shared_cluster=False)

    def test_a_failed_case_does_not_stop_the_series(self):
        cases = {"a": {"type": "locust"}, "b": {"type": "locust"}}
        ran = []

        def case(name, _):
            ran.append(name)
            if name == "a":
                raise locust.LocustStillRunning("unread")

        self.assertEqual(run.run_cases(self.args, cases, case, shared_cluster=False), ["a"])
        self.assertEqual(ran, ["a", "b"])
        with self.assertRaises(locust.LocustStillRunning):
            run.run_cases(self.args, cases, case, shared_cluster=True)

    def test_a_failed_destroy_stops_the_series(self):
        def case(name, _):
            raise deployment.DestroyFailed(Path("/x"), None)

        with self.assertRaises(deployment.DestroyFailed):
            run.run_cases(self.args, {"a": {"type": "locust"}, "b": {"type": "locust"}}, case, shared_cluster=False)


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

        from scenario_runner import ate_env, images, sandbox_benchmark, substrate

        args = argparse.Namespace(no_deploy_workloads=False, image_repo="repo")
        with mock.patch.object(images, "tag", return_value="t"), \
             mock.patch.object(substrate, "build_version", return_value="v"), \
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
        from scenario_runner.cases import SKIP_SUBSTRATE_INSTALL

        comparator = {"tests": ["test_x"], "runs": 1}
        case = {"type": "sandbox-benchmark", "comparator": comparator, SKIP_SUBSTRATE_INSTALL: True}
        needed = self.needed({"a": case})
        self.assertEqual(set(needed), {"env", "benchmark"})
        self.assertEqual(needed["env"], ["repo/env/ate-env-api:e", "repo/env/ate-env-guest:e"])
        cloud = {**case, "comparator": {**case["comparator"], "providers": ["e2b"]}}
        self.assertEqual(set(self.needed({"a": cloud})), {"benchmark"})

    def test_noop_needs_only_what_its_install_does(self):
        from scenario_runner.cases import SKIP_SUBSTRATE_INSTALL

        self.assertEqual(set(self.needed({"a": {"type": "noop"}})), {"substrate"})
        self.assertEqual(self.needed({"a": {"type": "noop", SKIP_SUBSTRATE_INSTALL: True}}), {})


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

        args = argparse.Namespace(name="run-1", cloud_build=False, no_deploy_workloads=False, report_bucket=None,
                                  report_prefix="reports", dev_infra=None)  # fmt: skip
        with mock.patch.object(benchmark, "run") as bench_run:
            run.run_case(args, "c-1", {"type": "locust"}, mock.Mock(), "c", 1, "repo")
        for type_, module in benchmark.RUNNERS.items():
            params = inspect.signature(module.run).parameters
            self.assertLessEqual(set(bench_run.call_args.kwargs), set(params), type_)
