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

"""A case of type swebench: what it may state, the dataset and loader
arguments it makes, what is deployed for it from sandboxenv-rl, and how the
rest of the runner is wired to it."""

import argparse
import base64
import json
import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import yaml

from scenario_runner import benchmarks, images
from scenario_runner.benchmarks import swebench
from scenario_runner.benchmarks.swebench import sandboxenv_rl
from scenario_runner.cli import run as cli_run
from scenario_runner.control_planes import substrate
from scenario_runner.images import builder
from scenario_runner.scenarios.file import SCENARIOS_FILE, load_scenarios

DIGEST = "sha256:" + "ab" * 32
# A file of this repository that a `dataset.file` can name.
SOME_FILE = "benchmarking/swebench/images.json"


def case(**options) -> dict:
    base = {"install": {"substrate": {"sandbox-class": "gvisor"}}, "dataset": {"images": ["swebench-smoke-rootfs"]}}
    return {"type": "swebench", "swebench": {**base, **options}}


def checked(**options) -> dict:
    return swebench.check_case("c", case(**options), True)["swebench"]


class CaseChecks(unittest.TestCase):
    def test_defaults_of_an_eval_case(self):
        options = checked()
        self.assertEqual(options["mode"], "eval")
        self.assertEqual((options["width"], options["tasks-per-image"]), (10, 1))
        self.assertEqual(options["env-worker-count"], 10)
        self.assertEqual((options["actor-cpu"], options["actor-memory"]), ("2", "4Gi"))
        # Image-only datasets have nothing to grade, so run the file-read workload.
        self.assertEqual((options["trajectory"], options["read-file"]), ("workload-file-read-one", "/etc/os-release"))
        self.assertEqual((options["validate"], options["loader-args"], options["max-images"]), (False, [], 0))
        for key in ("steps", "rollouts", "park"):
            self.assertNotIn(key, options)

    def test_defaults_of_a_steps_case(self):
        options = checked(mode="steps")
        self.assertEqual((options["steps"], options["rollouts"], options["park"]), (4, 2, "suspend"))
        self.assertNotIn("tasks-per-image", options)

    def test_what_is_stated_wins(self):
        options = checked(mode="steps", steps=2, park="fork", width=3, **{"actor-memory": "1Gi", "loader-args": ["-connections=2"]})
        self.assertEqual((options["steps"], options["park"], options["width"]), (2, "fork", 3))
        self.assertEqual((options["actor-memory"], options["loader-args"]), ("1Gi", ["-connections=2"]))

    def test_a_file_dataset_has_no_default_workload(self):
        options = checked(dataset={"file": SOME_FILE})
        self.assertEqual(options["dataset"], {"file": SOME_FILE})
        self.assertIsNone(options["trajectory"])
        self.assertIsNone(options["read-file"])
        self.assertEqual(checked(dataset={"file": SOME_FILE}, trajectory="swebench-gold")["trajectory"], "swebench-gold")

    def test_refuses(self):
        refused = {
            "an unknown key": ({"colour": 1}, "unknown colour"),
            "no dataset": ({"dataset": None}, "must be a mapping"),
            "both dataset sources": ({"dataset": {"file": SOME_FILE, "images": ["swebench-smoke-rootfs"]}}, "one of"),
            "an unknown image": ({"dataset": {"images": ["nope"]}}, "unknown nope"),
            "the loader as a task image": ({"dataset": {"images": ["swebench-loader"]}}, "unknown swebench-loader"),
            "no images": ({"dataset": {"images": []}}, "distinct image names"),
            "a repeated image": ({"dataset": {"images": ["swebench-smoke-rootfs"] * 2}}, "distinct image names"),
            "a missing file": ({"dataset": {"file": "benchmarking/swebench/none.jsonl"}}, "is not a file"),
            "a file outside the repository": ({"dataset": {"file": "../../../../etc/passwd"}}, "inside this repository"),
            "an unknown mode": ({"mode": "open-loop"}, "mode` must be one of"),
            "steps in eval": ({"steps": 3}, "`mode: steps`"),
            "tasks per image in steps": ({"mode": "steps", "tasks-per-image": 3}, "`mode: eval`"),
            "no width": ({"width": 0}, "width` must be a whole number from 1"),
            "a boolean width": ({"width": True}, "width` must be a whole number from 1"),
            "no workers": ({"env-worker-count": 0}, "env-worker-count` must be a whole number from 1"),
            "an unknown park": ({"mode": "steps", "park": "nap"}, "park` must be one of"),
            "a bad memory": ({"actor-memory": "lots"}, "actor-memory` must be a Kubernetes quantity"),
            "a numeric cpu": ({"actor-cpu": 2}, "actor-cpu` must be a quoted cpu quantity"),
            "a grading trajectory on images": ({"trajectory": "swebench-gold"}, "no grading material"),
            "a relative read file": ({"read-file": "etc/os-release"}, "absolute path"),
            "a read file without its workload": ({"dataset": {"file": SOME_FILE}, "read-file": "/etc/hosts"}, "is for"),
            "a managed flag": ({"loader-args": ["-width=3"]}, "names -width"),
            "a managed flag with two dashes": ({"loader-args": ["--set", "A=b"]}, "names -set"),
            "an output flag": ({"loader-args": ["-out=/x"]}, "names -out"),
            "loader args that are no list": ({"loader-args": "-connections=2"}, "must be a list"),
            "a validate that is no boolean": ({"validate": "yes"}, "validate` must be true or false"),
        }
        for what, (options, message) in refused.items():
            with self.subTest(what), self.assertRaisesRegex(ValueError, re.escape(message)):
                checked(**options)

    def test_refuses_a_case_without_substrate(self):
        no_install = case()
        del no_install["swebench"]["install"]
        agent_sandbox = case(install={"agent-sandbox": {"sandbox-class": "gvisor"}})
        for bad in (no_install, agent_sandbox):
            with self.assertRaisesRegex(ValueError, "ate-env-api runs on substrate"):
                swebench.check_case("c", bad, True)

    def test_refuses_a_file_over_a_configmap(self):
        with mock.patch.object(swebench, "MAX_DATASET_BYTES", 5), self.assertRaisesRegex(ValueError, "a ConfigMap holds"):
            checked(dataset={"file": SOME_FILE})

    def test_refuses_a_bad_name(self):
        with self.assertRaisesRegex(ValueError, "valid label value"):
            swebench.check_case("Not A Label", case(), True)

    def test_a_microvm_case_needs_kvm(self):
        microvm = case(install={"substrate": {"sandbox-class": "microvm"}})
        cluster = mock.Mock(sandboxed_kvm=False)
        with self.assertRaisesRegex(swebench.SwebenchError, "no /dev/kvm"):
            swebench.run(cluster, "c", microvm, image_repo="repo")


class Dataset(unittest.TestCase):
    def manifest(self, options: dict) -> tuple[str, dict]:
        with mock.patch.object(images, "pin", side_effect=lambda ref: f"{ref.split(':')[0]}@{DIGEST}"), \
             mock.patch.object(images, "tag", return_value="t"):  # fmt: skip
            return swebench.dataset_manifest(swebench.check_case("c", case(**options), True), "repo")

    def test_images_become_an_image_only_manifest_pinned_by_digest(self):
        text, refs = self.manifest({})
        image = f"repo/benchmark/swebench-smoke-rootfs@{DIGEST}"
        self.assertEqual(refs, {"swebench-smoke-rootfs": image})
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["instance_id"], rows[0]["image"]), ("swebench-smoke-rootfs", image))
        # What make-dataset.py --images-file writes, which the loader reads.
        self.assertEqual(
            set(rows[0]),
            {"instance_id", "repo", "image", "patch", "test_patch", "eval_script", "fail_to_pass", "pass_to_pass",
             "log_parser", "eval_type"},
        )  # fmt: skip
        self.assertTrue(text.endswith("\n"))

    def test_a_file_is_read_as_it_is(self):
        text, refs = self.manifest({"dataset": {"file": SOME_FILE}})
        self.assertEqual(text, (images.ROOT / SOME_FILE).read_text())
        self.assertEqual(refs, {})


class LoaderArguments(unittest.TestCase):
    RUN = "20261118t120000z-abcdef123456"

    def args(self, **options) -> list[str]:
        return swebench.loader_args("c", swebench.check_case("c", case(**options), True), self.RUN)

    def test_an_eval_run(self):
        args = self.args(width=4, **{"tasks-per-image": 12})
        self.assertEqual(
            args,
            [
                "-dataset=/data/dataset.jsonl",
                "-endpoint=ate-env-api.ate-env.svc.cluster.local:7777",
                "-atespace=ate-env",
                "-base-template=default-template",
                "-mode=eval",
                "-width=4",
                "-images=0",
                f"-run-id={swebench.loader_run_id(self.RUN, 'c')}",
                "-test-name=c",
                f"-tag={self.RUN}",
                "-tasks-per-image=12",
                "-trajectory=workload-file-read-one",
                "-set=READ_FILE=/etc/os-release",
            ],
        )

    def test_a_steps_run(self):
        args = self.args(mode="steps", steps=3, rollouts=5, park="suspend-async", validate=True)
        self.assertIn("-mode=steps", args)
        self.assertEqual([a for a in args if a.split("=")[0] in ("-steps", "-rollouts", "-park")],
                         ["-steps=3", "-rollouts=5", "-park=suspend-async"])  # fmt: skip
        self.assertNotIn("-tasks-per-image=1", args)
        self.assertEqual(args[-1], "-validate")

    def test_a_file_dataset_leaves_the_loaders_own_workload(self):
        args = self.args(dataset={"file": SOME_FILE}, **{"max-images": 5})
        self.assertIn("-images=5", args)
        self.assertFalse([a for a in args if a.startswith(("-trajectory", "-set"))])

    def test_extra_flags_come_last(self):
        self.assertEqual(self.args(**{"loader-args": ["-connections=2", "-step-timeout=1m"]})[-2:],
                         ["-connections=2", "-step-timeout=1m"])  # fmt: skip

    def test_the_loaders_run_id_is_a_short_label_of_the_run_and_record(self):
        run_id = swebench.loader_run_id(self.RUN, "a-case-with-a-rather-long-name-indeed")
        self.assertRegex(run_id, r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
        self.assertLessEqual(len(run_id), 20)
        self.assertEqual(run_id, swebench.loader_run_id(self.RUN, "a-case-with-a-rather-long-name-indeed"))
        self.assertNotEqual(run_id, swebench.loader_run_id(self.RUN, "another"))
        self.assertNotEqual(run_id, swebench.loader_run_id("another-run", "a-case-with-a-rather-long-name-indeed"))


class JobManifest(unittest.TestCase):
    VALUES = {
        "LOADER_IMAGE": f"repo/benchmark/swebench-loader@{DIGEST}",
        "BENCHMARK_RUNNER_KSA": "runner",
        "BENCHMARK_RUN_ID": "run-1",
        "BENCHMARK_RECORD_NAME": "c",
        "LOADER_ARGS": "QVJHUw==",
        "BENCHMARK_RUN_INFO": "SU5GTw==",
        "BENCHMARK_REPORT_BUCKET": "bucket",
        "BENCHMARK_REPORT_PREFIX": "reports",
        "BENCHMARK_REPORT_FOLDER": "c",
        "TOKEN_SECRET": swebench.TOKEN_SECRET,
        "DATASET_CONFIGMAP": swebench.DATASET_CONFIGMAP,
    }

    def render(self) -> list[dict]:
        return list(yaml.safe_load_all(swebench.render_job(self.VALUES)))

    def test_a_job_and_the_service_account_it_uploads_as(self):
        job, account = self.render()
        self.assertEqual((job["kind"], job["metadata"]["name"], job["metadata"]["namespace"]), ("Job", swebench.JOB, "benchmarking"))
        self.assertEqual(account["metadata"], {"name": "runner", "namespace": "benchmarking"})
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "runner")
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertEqual(job["spec"]["backoffLimit"], 0)

    def test_the_loader_gets_what_run_loader_reads(self):
        container = self.render()[0]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], self.VALUES["LOADER_IMAGE"])
        env = {e["name"]: e for e in container["env"]}
        # What run_loader.py's docstring lists.
        for name in ("LOADER_ARGS", "BENCHMARK_RUN_INFO", "BENCHMARK_RUN_ID", "REPORT_BUCKET", "REPORT_PREFIX",
                     "BENCHMARK_REPORT_FOLDER"):  # fmt: skip
            self.assertIn(name, env)
        self.assertEqual(env["REPORT_BUCKET"]["value"], "bucket")
        self.assertEqual(env["LOADER_ARGS"]["value"], "QVJHUw==")
        self.assertEqual(
            env["SUBSTRATE_ENV_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"], {"name": swebench.TOKEN_SECRET, "key": "token"}
        )

    def test_the_dataset_is_mounted_where_the_loader_is_told_to_read_it(self):
        pod = self.render()[0]["spec"]["template"]["spec"]
        mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
        self.assertEqual(swebench.DATASET_PATH, f"{mounts['dataset']}/{swebench.DATASET_KEY}")
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["dataset"]["configMap"]["name"], swebench.DATASET_CONFIGMAP)
        self.assertIn("out", mounts)

    def test_no_placeholder_is_left(self):
        self.assertNotIn("${", swebench.render_job(self.VALUES))


class ShippedCase(unittest.TestCase):
    CASE = load_scenarios(SCENARIOS_FILE).cases["swebench-substrate-gvisor"]

    def test_it_is_a_valid_swebench_case_on_one_node(self):
        self.assertEqual(self.CASE["type"], "swebench")
        options = swebench.check_case("swebench-substrate-gvisor", self.CASE, True)["swebench"]
        self.assertEqual(options["dataset"], {"images": ["swebench-smoke-rootfs"]})
        self.assertEqual(benchmarks.cluster_config("swebench-substrate-gvisor", self.CASE).sandboxed_nodes, 1)

    def test_its_width_fits_its_workers(self):
        options = self.CASE["swebench"]
        self.assertLessEqual(options["width"], options["env-worker-count"])

    def test_it_is_in_no_suite_until_it_has_run_on_a_cluster(self):
        for suite, names in load_scenarios(SCENARIOS_FILE).suites.items():
            self.assertNotIn("swebench-substrate-gvisor", names, suite)

    def test_the_type_asks_substrate_to_let_ate_env_api_through(self):
        self.assertEqual(benchmarks.ingress_spiffe_ids(self.CASE), ["spiffe://cluster.local/ns/ate-env/sa/ate-env-api"])
        self.assertEqual(benchmarks.ingress_spiffe_ids({"type": "locust"}), [])
        self.assertEqual(benchmarks.image_files(self.CASE), [images.SWEBENCH_IMAGES])
        self.assertIn("swebench", benchmarks.types())


STACK = """\
apiVersion: v1
kind: Namespace
metadata:
  name: ate-env
---
apiVersion: ate.dev/v1
kind: WorkerPool
metadata:
  labels:
    ate-env/workerpool: default-template-workerpool
  name: default-template-workerpool
  namespace: ate-env
spec:
  replicas: 3
  sandboxClasses:
  - configRef:
      name: gvisor-default
    name: gvisor
  workerImage: ko://go.ate.dev/substrate/cmd/ateworker-gvisor
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ate-env-api
  namespace: ate-env
spec:
  replicas: 1
"""


@contextmanager
def printing(text: str):
    """A cli that makes a command printing text, and the arguments it was asked for."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as file:
        file.write(text)
        file.flush()
        asked = []

        def cli(*args):
            asked.append(args)
            return ["cat", file.name]

        yield cli, asked


class Stack(unittest.TestCase):
    CLUSTER = mock.Mock(bucket="bucket")

    def test_the_pool_is_fitted_to_the_cluster_and_the_class(self):
        with printing(STACK) as (cli, asked), \
             mock.patch.object(substrate, "resolve", side_effect=lambda cluster, manifest: manifest):  # fmt: skip
            stack = sandboxenv_rl.render_stack(cli, self.CLUSTER, "microvm", 8, f"repo/api@{DIGEST}", 2)
        self.assertEqual(
            asked,
            [(
                "manifest",
                "--namespace=ate-env",
                "--template=default-template",
                f"--api-image=repo/api@{DIGEST}",
                "--api-replicas=2",
                "--worker-image=ko://go.ate.dev/substrate/cmd/ateworker-microvm",
                "--replicas=8",
            )],
        )  # fmt: skip
        docs = {d["kind"]: d for d in yaml.safe_load_all(stack) if d}
        self.assertEqual(set(docs), {"Namespace", "WorkerPool", "Deployment"})
        spec = docs["WorkerPool"]["spec"]
        self.assertEqual(spec["sandboxClasses"], [{"name": "microvm", "configRef": {"name": "microvm"}}])
        self.assertEqual(spec["template"]["nodeSelector"], {"benchmarking-worker": "true"})
        self.assertEqual(spec["template"]["tolerations"][0]["key"], "ate.dev/sandboxClass")
        # Kept: the template selects the pool by this.
        self.assertEqual(docs["WorkerPool"]["metadata"]["labels"], {"ate-env/workerpool": "default-template-workerpool"})

    def test_the_stack_is_given_to_substrate_to_pin(self):
        with printing(STACK) as (cli, _), mock.patch.object(substrate, "resolve", return_value="pinned") as resolve:
            self.assertEqual(sandboxenv_rl.render_stack(cli, self.CLUSTER, "gvisor", 3, "api", 1), "pinned")
        self.assertIn("ko://go.ate.dev/substrate/cmd/ateworker-gvisor", resolve.call_args.args[1])

    def test_the_template_has_the_guest_and_the_size(self):
        with printing("template") as (cli, asked):
            self.assertEqual(sandboxenv_rl.render_template(cli, "guest@d", "2", "1Gi"), "template")
        self.assertEqual(
            asked,
            [(
                "manifest",
                "template",
                "--template=default-template",
                "--atespace=ate-env",
                "--guest-image=guest@d",
                "--cpu=2",
                "--memory=1Gi",
            )],
        )  # fmt: skip

    def test_the_names_agree_with_what_ate_env_manifest_writes(self):
        self.assertEqual(sandboxenv_rl.POOL, "default-template-workerpool")
        self.assertEqual(sandboxenv_rl.API_ADDR, "ate-env-api.ate-env.svc.cluster.local:7777")
        self.assertEqual(sandboxenv_rl.API_SPIFFE_ID, "spiffe://cluster.local/ns/ate-env/sa/ate-env-api")

    def test_the_token_is_read_from_its_secret(self):
        found = mock.Mock(returncode=0, stdout=base64.b64encode(b"tok").decode() + "\n")
        with mock.patch.object(sandboxenv_rl.subprocess, "run", return_value=found):
            self.assertEqual(sandboxenv_rl.auth_token(self.CLUSTER), "tok")
        for missing in (mock.Mock(returncode=1, stdout=""), mock.Mock(returncode=0, stdout="")):
            with mock.patch.object(sandboxenv_rl.subprocess, "run", return_value=missing):
                self.assertIsNone(sandboxenv_rl.auth_token(self.CLUSTER))

    def test_an_existing_token_is_kept(self):
        with mock.patch.object(sandboxenv_rl, "auth_token", return_value="tok"), \
             mock.patch.object(sandboxenv_rl, "stream") as stream:  # fmt: skip
            sandboxenv_rl.ensure_auth_secret(mock.Mock(), self.CLUSTER)
        stream.assert_not_called()

    def test_a_missing_token_is_made_once(self):
        cluster = mock.Mock()
        cluster.kubectl.side_effect = lambda *args, **kw: ["kubectl", *args]
        with printing("kind: Secret") as (cli, asked), \
             mock.patch.object(sandboxenv_rl, "auth_token", return_value=None), \
             mock.patch.object(sandboxenv_rl, "stream") as stream:  # fmt: skip
            sandboxenv_rl.ensure_auth_secret(cli, cluster)
        self.assertEqual(asked, [("manifest", "auth-secret", "--namespace=ate-env")])
        stream.assert_called_once_with(["kubectl", "create", "-f", "-"], input="kind: Secret")

    def test_the_access_policy_is_made_unless_the_atespace_has_one(self):
        def ate(*args):
            return ["echo", "bindings: []"] if args[0] == "get" else ["ate", *args]

        with mock.patch.object(sandboxenv_rl, "stream") as stream:
            sandboxenv_rl.ensure_access_policy(ate, "policy")
        stream.assert_not_called()

        def ate_without(*args):
            return ["true"] if args[0] == "get" else ["ate", *args]

        with mock.patch.object(sandboxenv_rl, "stream") as stream:
            sandboxenv_rl.ensure_access_policy(ate_without, "policy")
        stream.assert_called_once_with(["ate", "create", "access-policy", "-a", "ate-env", "-f", "-"], input="policy")

    def test_images_are_named_as_the_builder_pushes_them(self):
        with mock.patch.object(sandboxenv_rl, "build_version", return_value="v0.1.0-1-gabc"):
            self.assertEqual(
                sandboxenv_rl.image_refs("repo"),
                ["repo/sandboxenv-rl/ate-env-api:v0.1.0-1-gabc", "repo/sandboxenv-rl/ate-env-guest:v0.1.0-1-gabc"],
            )
            with mock.patch.object(images, "pin", side_effect=lambda ref: f"pinned:{ref}"):
                self.assertEqual(
                    sandboxenv_rl.pinned_image("repo", "ate-env-api"), "pinned:repo/sandboxenv-rl/ate-env-api:v0.1.0-1-gabc"
                )


class Pin(unittest.TestCase):
    """The submodule has to be the commit the repo pins."""

    @contextmanager
    def checkout(self, entry: str, head: str):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "go.mod").write_text("module x\n")
            with mock.patch.object(sandboxenv_rl, "DIR", Path(tmp)), \
                 mock.patch.object(sandboxenv_rl, "output", side_effect=[entry, ""]), \
                 mock.patch.object(sandboxenv_rl.git, "head", return_value=head):  # fmt: skip
                yield

    def test_the_pinned_commit_passes(self):
        with self.checkout("160000 abc123 0\tdependency-repos/sandboxenv-rl", "abc123"):
            sandboxenv_rl.check_pinned()

    def test_another_commit_is_refused(self):
        with self.checkout("160000 abc123 0\tdependency-repos/sandboxenv-rl", "def456"):
            with self.assertRaisesRegex(sandboxenv_rl.SandboxenvError, "pins abc123"):
                sandboxenv_rl.check_pinned()

    def test_a_repo_that_pins_nothing_is_refused(self):
        with self.checkout("", "abc123"), self.assertRaisesRegex(sandboxenv_rl.SandboxenvError, "pins no submodule"):
            sandboxenv_rl.check_pinned()

    def test_a_submodule_that_is_not_checked_out_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sandboxenv_rl, "DIR", Path(tmp)):
            with self.assertRaisesRegex(sandboxenv_rl.SandboxenvError, "git submodule update --init"):
                sandboxenv_rl.check_pinned()
            self.assertTrue([m for m in sandboxenv_rl.missing_requirements() if "sandboxenv-rl submodule" in m])


class Orchestration(unittest.TestCase):
    """What run_loader makes of the cluster, with its kubectl and Substrate
    calls recorded rather than made."""

    def setUp(self):
        self.calls = []
        self.cluster = mock.Mock(runner_ksa="runner")
        self.cluster.kubectl.side_effect = lambda *args, namespace=None, **kw: [
            "kubectl", *args, *([f"-n={namespace}"] if namespace else []),
        ]  # fmt: skip
        self.checked = swebench.check_case("c", case(), True)
        patches = {
            "deploy": mock.patch.object(sandboxenv_rl, "deploy"),
            "auth_token": mock.patch.object(sandboxenv_rl, "auth_token", return_value="tok"),
            "build_version": mock.patch.object(sandboxenv_rl, "build_version", return_value="v"),
            # Not git, which the subprocess.run patched below would take too.
            "commit": mock.patch.object(swebench, "commit", return_value="abc123"),
            "template": mock.patch.object(swebench.workloads, "deployed_template", return_value={"t": 1}),
            "output": mock.patch.object(swebench, "output", return_value="gvisor\n"),
            "collect": mock.patch.object(swebench.run_info, "collect", return_value={"info": True}),
            "dataset": mock.patch.object(swebench, "dataset_manifest", return_value=('{"a":1}\n', {"swebench-smoke-rootfs": "ref"})),
            "stream": mock.patch.object(swebench, "stream", side_effect=lambda cmd, **kw: self.calls.append((cmd, kw.get("input")))),
            "run": mock.patch.object(swebench.subprocess, "run", side_effect=lambda cmd, **kw: self.calls.append((cmd, None))),
            "wait_started": mock.patch.object(swebench, "wait_started"),
            "wait_for_job": mock.patch.object(swebench, "wait_for_job"),
            "follow": mock.patch.object(swebench.jobs, "follow_logs", side_effect=self.follow),
            "cleanup": mock.patch.object(swebench.reports, "record_cleanup"),
            "logs": mock.patch.object(swebench.reports, "upload_logs"),
        }
        self.mocks = {name: patch.start() for name, patch in patches.items()}
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    @contextmanager
    def follow(*args):
        yield lambda grace: None

    def run_loader(self, deploy: bool = True):
        swebench.run_loader(self.cluster, "c", self.checked, "run-1", "c", "loader@d", "repo", deploy, "bucket", "reports")

    def applied(self, kind: str) -> dict:
        for cmd, body in self.calls:
            if cmd[:2] == ["kubectl", "apply"] and body and f'"kind": "{kind}"' in body:
                return json.loads(body)
        self.fail(f"no {kind} applied")

    def test_it_deploys_with_the_cases_sizes(self):
        self.run_loader()
        self.mocks["deploy"].assert_called_once_with(self.cluster, "repo", "gvisor", 10, "4Gi", "2", 1)

    def test_it_leaves_the_deployment_alone_without_deploy_workloads(self):
        self.run_loader(deploy=False)
        self.mocks["deploy"].assert_not_called()

    def test_the_token_and_the_dataset_go_to_the_jobs_namespace(self):
        self.run_loader()
        secret = self.applied("Secret")
        self.assertEqual(secret["metadata"], {"name": swebench.TOKEN_SECRET, "namespace": "benchmarking"})
        self.assertEqual(base64.b64decode(secret["data"]["token"]), b"tok")
        configmap = self.applied("ConfigMap")
        self.assertEqual(configmap["data"], {"dataset.jsonl": '{"a":1}\n'})
        # Server-side, so neither is copied into an annotation.
        server_side = [cmd for cmd, body in self.calls if cmd[:3] == ["kubectl", "apply", "--server-side"]]
        self.assertEqual(len(server_side), 2)

    def test_the_job_is_rendered_with_the_loader_and_its_arguments(self):
        self.run_loader()
        job = next(body for cmd, body in self.calls if cmd == ["kubectl", "apply", "-f", "-"])
        container = yaml.safe_load(job.split("\n---\n")[0])["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], "loader@d")
        env = {e["name"]: e.get("value") for e in container["env"]}
        args = json.loads(base64.b64decode(env["LOADER_ARGS"]))
        self.assertEqual(args, swebench.loader_args("c", self.checked, "run-1"))
        info = json.loads(base64.b64decode(env["BENCHMARK_RUN_INFO"]))
        self.assertEqual(info["loader_args"], args)
        self.assertEqual((info["sandboxenv_rl_commit"], info["sandboxenv_rl_version"]), ("abc123", "v"))
        self.assertEqual(info["dataset"]["instances"], 1)
        self.assertEqual((env["REPORT_BUCKET"], env["REPORT_PREFIX"], env["BENCHMARK_REPORT_FOLDER"]), ("bucket", "reports", "c"))

    def test_the_secret_and_the_configmap_are_deleted_and_the_run_cleaned_up(self):
        self.run_loader()
        deleted = [cmd[2:4] for cmd, _ in self.calls if cmd[:2] == ["kubectl", "delete"] and cmd[2] in ("secret", "configmap")]
        self.assertEqual(deleted, [["secret", swebench.TOKEN_SECRET], ["configmap", swebench.DATASET_CONFIGMAP]])
        self.mocks["cleanup"].assert_called_once_with(self.cluster, "ate-env", "bucket", "reports", "run-1", "c")
        self.mocks["logs"].assert_called_once_with(self.cluster, "benchmarking", (swebench.JOB,), "bucket", "reports", "run-1", "c")

    def test_a_failed_job_still_cleans_up_and_uploads_the_log(self):
        self.mocks["wait_for_job"].side_effect = swebench.SwebenchError("failed")
        with self.assertRaises(swebench.SwebenchError):
            self.run_loader()
        self.mocks["cleanup"].assert_called_once()
        self.mocks["logs"].assert_called_once()
        deleted = [cmd[2] for cmd, _ in self.calls if cmd[:2] == ["kubectl", "delete"]]
        self.assertIn("secret", deleted)

    def test_a_job_still_running_is_left_alone(self):
        self.mocks["wait_for_job"].side_effect = swebench.SwebenchTimeout("timed out")
        with self.assertRaises(swebench.SwebenchTimeout):
            self.run_loader()
        # The loader deletes its environments itself, and may still be at it.
        self.mocks["cleanup"].assert_not_called()
        self.mocks["logs"].assert_called_once()

    def test_a_pool_of_another_class_is_refused_before_anything_is_made(self):
        self.mocks["output"].return_value = "microvm\n"
        with self.assertRaisesRegex(swebench.SwebenchError, "is microvm, not gvisor"):
            self.run_loader(deploy=False)
        self.assertEqual(self.calls, [])

    def test_without_a_token_it_says_to_deploy_again(self):
        self.mocks["auth_token"].return_value = None
        with self.assertRaisesRegex(swebench.SwebenchError, "no token in ate-env/ate-env-api-token"):
            self.run_loader(deploy=False)
        self.assertEqual(self.calls, [])


class Wiring(unittest.TestCase):
    """What the rest of the runner does for the type."""

    def test_the_builder_has_a_group_for_it(self):
        self.assertIn("sandboxenv-rl", builder.GROUPS)
        with mock.patch.object(sandboxenv_rl, "build_version", return_value="v"):
            self.assertEqual(
                builder.refs("sandboxenv-rl", "repo"), ["repo/sandboxenv-rl/ate-env-api:v", "repo/sandboxenv-rl/ate-env-guest:v"]
            )
            self.assertFalse(builder.dirty("sandboxenv-rl"))
        with mock.patch.object(sandboxenv_rl, "build_version", return_value="v-dirty"):
            self.assertTrue(builder.dirty("sandboxenv-rl"))

    def test_its_benchmark_images_are_built_with_the_others(self):
        with mock.patch.object(images, "tag", return_value="t"):
            refs = builder.refs("benchmark", "repo")
        self.assertIn("repo/benchmark/swebench-loader:t", refs)
        self.assertIn("repo/benchmark/swebench-smoke-rootfs:t", refs)

    def test_building_it_is_a_ko_build_in_the_submodule(self):
        with mock.patch.object(builder, "stream") as stream, \
             mock.patch.object(builder, "access_token", return_value="token"), \
             mock.patch.object(sandboxenv_rl, "build_version", return_value="v"), \
             mock.patch.object(builder, "LOGGED_IN", set()):  # fmt: skip
            builder.build_sandboxenv_rl("host-docker.pkg.dev/p/r", login=True)
        login, build = stream.call_args_list
        self.assertEqual(login.args[0][:4], ["go", "run", builder.KO, "login"])
        self.assertEqual(login.kwargs["input"], "token")
        self.assertEqual(build.args[0][:5], ["go", "run", builder.KO, "build", "--base-import-paths"])
        self.assertEqual(build.args[0][-2:], ["./cmd/ate-env-api", "./cmd/ate-env-guest"])
        self.assertIn("--tags=v", build.args[0])
        self.assertEqual(build.kwargs["env"]["KO_DOCKER_REPO"], "host-docker.pkg.dev/p/r/sandboxenv-rl")
        self.assertEqual(build.kwargs["cwd"], sandboxenv_rl.DIR)

    def test_a_missing_submodule_is_a_missing_requirement_of_its_group_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sandboxenv_rl, "DIR", Path(tmp)):
            self.assertTrue([m for m in builder.missing_requirements(["sandboxenv-rl"]) if "sandboxenv-rl submodule" in m])
            self.assertFalse([m for m in builder.missing_requirements(["benchmark"]) if "sandboxenv-rl submodule" in m])

    def needed(self, cases: dict, no_deploy_workloads: bool = False) -> dict:
        args = argparse.Namespace(no_deploy_workloads=no_deploy_workloads, image_repo="repo")
        with mock.patch.object(images, "tag", return_value="t"), \
             mock.patch.object(substrate, "build_version", return_value="v"), \
             mock.patch.object(sandboxenv_rl, "build_version", return_value="s"):  # fmt: skip
            return cli_run.needed_images(args, cases)

    def test_a_swebench_case_needs_substrate_sandboxenv_rl_and_its_images(self):
        needed = self.needed({"a": {"type": "swebench", **case()}})
        self.assertEqual(set(needed), {"substrate", "sandboxenv-rl", "benchmark"})
        self.assertEqual(needed["sandboxenv-rl"], ["repo/sandboxenv-rl/ate-env-api:s", "repo/sandboxenv-rl/ate-env-guest:s"])
        with mock.patch.object(images, "tag", return_value="t"):
            self.assertEqual(needed["benchmark"], images.refs("repo", [images.SWEBENCH_IMAGES]))

    def test_it_needs_no_sandboxenv_rl_when_the_deployment_is_not_the_runs(self):
        needed = self.needed({"a": {"type": "swebench", **case()}}, no_deploy_workloads=True)
        self.assertNotIn("sandboxenv-rl", needed)

    def test_other_types_do_not_need_it(self):
        locust = {"type": "locust", "locust": {"install": {"substrate": {"sandbox-class": "gvisor"}}}}
        self.assertNotIn("sandboxenv-rl", self.needed({"a": locust}))


if __name__ == "__main__":
    unittest.main()
