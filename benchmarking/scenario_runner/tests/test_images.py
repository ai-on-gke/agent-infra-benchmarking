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

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scenario_runner import images


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=repo, check=True,
                   capture_output=True)  # fmt: skip


class ChangesDigest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q")
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / ".gitignore").write_text("ignored/\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "c")

    def test_clean_tree(self):
        self.assertEqual(images.changes_digest(self.repo), "")

    def test_ignored_files_do_not_count(self):
        (self.repo / "ignored").mkdir()
        (self.repo / "ignored" / "x").write_text("x")
        self.assertEqual(images.changes_digest(self.repo), "")

    def test_changes_and_untracked_files_count_by_content(self):
        (self.repo / "a.txt").write_text("b\n")
        changed = images.changes_digest(self.repo)
        self.assertTrue(changed)
        (self.repo / "new.txt").write_text("1")
        with_new = images.changes_digest(self.repo)
        self.assertNotEqual(changed, with_new)
        (self.repo / "new.txt").write_text("2")
        self.assertNotEqual(with_new, images.changes_digest(self.repo))


class Tag(unittest.TestCase):
    def tag(self, root_changes: str, submodule_changes: str) -> str:
        images.tag.cache_clear()
        digests = {images.ROOT: root_changes}
        with mock.patch.object(images, "commit", return_value="c" * 40), \
             mock.patch.object(images, "changes_digest", side_effect=lambda repo: digests.get(repo, submodule_changes)):
            try:
                return images.tag()
            finally:
                images.tag.cache_clear()

    def test_a_clean_tree_is_tagged_with_its_commit(self):
        self.assertEqual(self.tag("", ""), "c" * 40)

    def test_a_dirty_tree_is_tagged_with_its_changes(self):
        tag = self.tag("abc", "")
        self.assertRegex(tag, rf"^{'c' * 40}-dirty-[0-9a-f]{{{images.DIRTY_DIGEST_LENGTH}}}$")
        self.assertNotEqual(tag, self.tag("abd", ""))
        self.assertNotEqual(tag, self.tag("abc", "sub"))
        # A Docker tag takes 128 characters, and the commit label 63.
        self.assertLessEqual(len(tag), 63)


def described(digests: dict[str, str]):
    """gcloud artifacts docker images describe, serving digests by image."""

    def run(cmd, **kwargs):
        value = digests.get(cmd[5], "")
        return subprocess.CompletedProcess(cmd, 0 if value else 1, stdout=value + "\n", stderr="")

    return mock.patch.object(images.subprocess, "run", side_effect=run)


class Refs(unittest.TestCase):
    def test_pin_keeps_a_registry_port(self):
        with described({"host:5000/p/r/img:tag": "sha256:abc"}):
            self.assertEqual(images.pin("host:5000/p/r/img:tag"), "host:5000/p/r/img@sha256:abc")

    def test_pin_refuses_what_it_cannot_resolve(self):
        with described({}), self.assertRaises(images.ImageError):
            images.pin("us-docker.pkg.dev/p/r/img:tag")

    def test_missing_names_the_images_this_tree_has_not_pushed(self):
        repo = "us-docker.pkg.dev/p/r"
        files = [images.LOCUST_IMAGES, images.WORKLOADS_IMAGES]
        names = [image.name for image in images.read_images(*files)]
        with mock.patch.object(images, "tag", return_value="t"):
            refs = images.refs(repo, files)
        self.assertEqual(refs, [f"{repo}/benchmark/{name}:t" for name in names])
        pushed = {ref: "sha256:abc" for ref in refs}
        with described(pushed):
            self.assertEqual(images.missing(refs), [])
        del pushed[refs[0]]
        with described(pushed):
            self.assertEqual(images.missing(refs), [refs[0]])

    def test_repository_name(self):
        self.assertEqual(
            images.repository_name("us-central1-docker.pkg.dev/my-project/my-repo"),
            "projects/my-project/locations/us-central1/repositories/my-repo",
        )
        for bad in ("us-central1-docker.pkg.dev/my-project", "gcr.io/my-project/my-repo", "x-docker.pkg.dev/p/r/extra"):
            with self.assertRaises(ValueError):
                images.repository_name(bad)

    def test_builders_are_pinned_by_digest(self):
        from scenario_runner import builder

        for image in (builder.CLOUD_BUILDER, builder.GO_BUILDER):
            self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")
