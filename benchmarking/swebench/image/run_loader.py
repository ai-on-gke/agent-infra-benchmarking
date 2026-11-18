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

"""Runs swebench-loader in the Job of a swebench case and uploads what it wrote.

It runs `swebench-loader run` with LOADER_ARGS, adding where the loader writes
its files, summarizes the events with `swebench-loader report`, writes run.json
and uploads everything under OUT_DIR to
gs://REPORT_BUCKET/REPORT_PREFIX/BENCHMARK_RUN_ID/BENCHMARK_REPORT_FOLDER/, whatever
the loader's exit status, so a failed run's events are kept. Its own exit
status is the loader's, or 1 if the upload failed.

It needs only the standard library: the access token comes from the metadata
server, as the Job's service account has the bucket's objectCreator grant, and
the upload is a call of the Cloud Storage JSON API, which that grant allows.

The environment:

  LOADER_ARGS            base64 of the JSON list of the arguments after `run`.
                         The scenario-runner owns what the loader writes, so it
                         may not name -out, -stats-out or -run-window.
  BENCHMARK_RUN_INFO     base64 of the JSON run.json starts from.
  REPORT_BUCKET          the bucket; without it nothing is uploaded.
  REPORT_PREFIX, BENCHMARK_RUN_ID, BENCHMARK_REPORT_FOLDER   where in it.
  OUT_DIR                where the loader writes, /out by default.
  SWEBENCH_LOADER        the loader binary, /usr/local/bin/swebench-loader.
  STORAGE_API            the Cloud Storage endpoint, for a test's fake of it.
  STORAGE_ACCESS_TOKEN   a token to use instead of the metadata server's.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EVENTS = "events.jsonl"
STATS = "stats.jsonl"
RUN_WINDOW = "run_window.json"
SUMMARY = "summary.json"
REPORT_TEXT = "report.txt"
RUN_JSON = "run.json"
# What the loader is told to write, and so what LOADER_ARGS may not name.
OWNED_FLAGS = ("out", "stats-out", "run-window")
METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
UPLOAD_ATTEMPTS = 4
CONTENT_TYPES = {".json": "application/json", ".jsonl": "application/x-ndjson", ".txt": "text/plain", ".log": "text/plain"}


def log(message: str) -> None:
    print(f"run_loader: {message}", flush=True)


def decode_json(name: str, default: object = None) -> object:
    """The base64 JSON in the environment variable name."""
    value = os.environ.get(name, "")
    if not value:
        return default
    try:
        return json.loads(base64.b64decode(value, validate=True))
    except ValueError as exc:
        raise SystemExit(f"run_loader: {name} is not base64 of JSON: {exc}") from None


def loader_args(out_dir: Path) -> list[str]:
    """LOADER_ARGS with the loader's output files, which this owns, added."""
    args = decode_json("LOADER_ARGS", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise SystemExit("run_loader: LOADER_ARGS must be a JSON list of strings")
    for arg in args:
        flag = arg.lstrip("-").split("=", 1)[0]
        if arg.startswith("-") and flag in OWNED_FLAGS:
            raise SystemExit(f"run_loader: LOADER_ARGS names -{flag}, which run_loader.py sets")
    return [
        *args,
        f"-out={out_dir / EVENTS}",
        f"-stats-out={out_dir / STATS}",
        f"-run-window={out_dir / RUN_WINDOW}",
    ]


def run_loader(binary: str, args: list[str]) -> int:
    """Runs the loader to its end and returns its exit status. A SIGTERM, as a
    deleted Job's pod gets, goes on to it, which ends its run and writes its
    events, and this carries on to upload them."""
    proc = subprocess.Popen([binary, "run", *args])

    def forward(signum: int, _frame: object) -> None:
        log(f"got signal {signum}; passing it to the loader so it ends the run and writes its events")
        proc.send_signal(signum)

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        code = proc.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    # A process killed by a signal has a negative status; a shell's is 128 and the signal.
    return 128 - code if code < 0 else code


def summarize(binary: str, out_dir: Path) -> None:
    """The loader's own report of the events, as text in the log and
    report.txt and as summary.json. Best-effort: the events are the record."""
    events = out_dir / EVENTS
    if not events.is_file():
        log(f"no {EVENTS} to summarize")
        return
    proc = subprocess.run([binary, "report", str(events), "-json", str(out_dir / SUMMARY)], capture_output=True, text=True)
    text = proc.stdout + proc.stderr
    print(text, end="" if text.endswith("\n") else "\n", flush=True)
    (out_dir / REPORT_TEXT).write_text(text)
    if proc.returncode:
        log(f"swebench-loader report exited {proc.returncode}")


def write_run_json(out_dir: Path, started: datetime, ended: datetime, code: int) -> None:
    info = decode_json("BENCHMARK_RUN_INFO", {})
    if not isinstance(info, dict):
        info = {"run_info": info}
    info.update(
        {
            "loader_started_at": started.isoformat(timespec="seconds"),
            "loader_ended_at": ended.isoformat(timespec="seconds"),
            "loader_exit_code": code,
        }
    )
    (out_dir / RUN_JSON).write_text(json.dumps(info, indent=2) + "\n")


def access_token() -> str:
    token = os.environ.get("STORAGE_ACCESS_TOKEN")
    if token:
        return token
    request = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["access_token"]


def upload_file(api: str, token: str, bucket: str, name: str, path: Path) -> None:
    """One object, retried: the access token and the network both fail now and then."""
    size = path.stat().st_size
    url = f"{api}/upload/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o?" + urllib.parse.urlencode(
        {"uploadType": "media", "name": name}
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
        "Content-Length": str(size),
    }
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            with path.open("rb") as body:
                with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"), timeout=300):
                    return
        except urllib.error.HTTPError as exc:
            # A client error other than a throttle or a timeout is the same next time.
            if attempt == UPLOAD_ATTEMPTS or (400 <= exc.code < 500 and exc.code not in (408, 429)):
                raise RuntimeError(f"{name}: HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from None
        except (urllib.error.URLError, OSError) as exc:
            if attempt == UPLOAD_ATTEMPTS:
                raise RuntimeError(f"{name}: {exc}") from None
        time.sleep(2**attempt)


def upload(out_dir: Path) -> bool:
    """Uploads every file under out_dir; whether they all went."""
    bucket = os.environ.get("REPORT_BUCKET", "")
    if not bucket:
        log("no REPORT_BUCKET, so nothing is uploaded")
        return True
    prefix = "/".join(
        os.environ[key].strip("/") for key in ("REPORT_PREFIX", "BENCHMARK_RUN_ID", "BENCHMARK_REPORT_FOLDER") if os.environ.get(key)
    )
    api = os.environ.get("STORAGE_API", "https://storage.googleapis.com").rstrip("/")
    try:
        token = access_token()
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        log(f"cannot get an access token: {exc}")
        return False
    ok = True
    for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        name = f"{prefix}/{path.relative_to(out_dir).as_posix()}" if prefix else path.relative_to(out_dir).as_posix()
        try:
            upload_file(api, token, bucket, name, path)
        except RuntimeError as exc:
            log(f"could not upload {exc}")
            ok = False
        else:
            log(f"uploaded gs://{bucket}/{name}")
    return ok


def main() -> int:
    out_dir = Path(os.environ.get("OUT_DIR", "/out"))
    binary = os.environ.get("SWEBENCH_LOADER", "/usr/local/bin/swebench-loader")
    out_dir.mkdir(parents=True, exist_ok=True)
    args = loader_args(out_dir)
    log(f"running {binary} run {' '.join(args)}")
    started = datetime.now(timezone.utc)
    code = run_loader(binary, args)
    ended = datetime.now(timezone.utc)
    log(f"the loader exited {code} after {(ended - started).total_seconds():.0f}s")
    summarize(binary, out_dir)
    write_run_json(out_dir, started, ended, code)
    if not upload(out_dir):
        return code or 1
    return code


if __name__ == "__main__":
    sys.exit(main())
