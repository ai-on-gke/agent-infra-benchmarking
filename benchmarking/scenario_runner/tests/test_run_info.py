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

import unittest
from unittest import mock

from scenario_runner import run_info


def pod(
    name: str, owner: dict | None, statuses: list[dict], template_hash: str | None = None, spec: dict | None = None
) -> dict:
    labels = {"pod-template-hash": template_hash} if template_hash else {}
    return {
        "metadata": {"name": name, "labels": labels, "ownerReferences": [owner] if owner else []},
        "spec": {"containers": [{"name": c, "image": image} for c, image in (spec or {}).items()]},
        "status": {"containerStatuses": statuses},
    }


class ControlPlaneImages(unittest.TestCase):
    def test_images_by_workload_with_the_digests_that_ran(self):
        pods = [
            pod("ateapi-7d9f-abc", {"kind": "ReplicaSet", "name": "ateapi-7d9f"},
                [{"name": "ateapi", "image": "r/ateapi:v1", "imageID": "r/ateapi@sha256:1"}], "7d9f"),
            pod("atelet-x", {"kind": "DaemonSet", "name": "atelet"},
                [{"name": "atelet", "image": "r/atelet:v1", "imageID": "r/atelet@sha256:2"}]),
            pod("atelet-y", {"kind": "DaemonSet", "name": "atelet"},
                [{"name": "atelet", "image": "r/atelet:v1", "imageID": "r/atelet@sha256:3"}]),
            pod("bare", None, [{"name": "c", "image": "i", "imageID": ""}]),
            pod("atenet-z", {"kind": "DaemonSet", "name": "atenet"},
                [{"name": "atenet", "image": "sha256:9", "imageID": "r/atenet@sha256:4"}],
                spec={"atenet": "r/atenet@sha256:4"}),
        ]  # fmt: skip
        with mock.patch.object(run_info, "_kubectl_json", return_value={"items": pods}):
            images = run_info.pod_images(mock.Mock(), "ate-system")
        self.assertEqual(images["Deployment/ateapi"]["ateapi"], {"image": "r/ateapi:v1", "digests": ["sha256:1"]})
        # Pods of one DaemonSet that run different digests show both.
        self.assertEqual(images["DaemonSet/atelet"]["atelet"]["digests"], ["sha256:2", "sha256:3"])
        self.assertEqual(images["Pod/bare"]["c"]["digests"], [])
        # Named as the manifest named it, not by the image ID a digest reference shows in the status.
        self.assertEqual(images["DaemonSet/atenet"]["atenet"]["image"], "r/atenet@sha256:4")


class Tools(unittest.TestCase):
    def test_versions_are_read_where_the_tools_are(self):
        versions = run_info.tools()
        self.assertEqual(set(versions), {"terraform", "kubectl", "gcloud", "go", "docker", "ko", "python"})
        self.assertRegex(versions["ko"], r"^v\d")

    def test_builders(self):
        self.assertEqual(run_info.builders(False), {"where": "local docker"})
        self.assertEqual(set(run_info.builders(True)), {"where", "docker", "go"})


class MissingTool(unittest.TestCase):
    def test_a_missing_tool_has_no_version(self):
        with mock.patch.object(run_info.subprocess, "run", side_effect=FileNotFoundError("docker")):
            self.assertIsNone(run_info._version(["docker", "version"]))
