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

"""Upload reports to the deployment's GCS bucket.

The pod's filesystem goes when the Job's TTL expires, so the bucket is where
a report outlives the run that produced it. Credentials come from Workload
Identity on the benchmark-runner ServiceAccount; see the deployment notes in
the repo README.
"""

import mimetypes
from pathlib import Path

# Default subfolder under the bucket root. One folder per run beneath it, so
# a run's files stay together and two runs cannot collide.
DEFAULT_PREFIX = "reports"


class UploadError(Exception):
    pass


def object_name(prefix: str, run_id: str, filename: str) -> str:
    return f"{prefix.strip('/')}/{run_id}/{filename}"


def upload(
    paths: list[Path], bucket_name: str, run_id: str, prefix: str = DEFAULT_PREFIX
) -> list[tuple[Path, str | None, str | None]]:
    """Upload each path. Returns (path, gs_uri, error) per file.

    The runner grants this objectCreator and nothing more, so an object that
    already exists is a failure rather than an overwrite. Run ids are unique
    by default, which is what keeps that from happening.
    """
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise UploadError(f"google-cloud-storage is not installed: {exc}") from exc

    try:
        bucket = storage.Client().bucket(bucket_name)
    except Exception as exc:
        raise UploadError(f"cannot reach bucket {bucket_name}: {exc}") from exc

    results = []
    for path in paths:
        name = object_name(prefix, run_id, path.name)
        try:
            blob = bucket.blob(name)
            content_type, _ = mimetypes.guess_type(path.name)
            blob.upload_from_filename(path, content_type=content_type)
        except Exception as exc:
            results.append((path, None, str(exc)))
        else:
            results.append((path, f"gs://{bucket_name}/{name}", None))
    return results
