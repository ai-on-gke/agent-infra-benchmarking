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

"""Runs one benchmark to completion and exits with its result.

locust runs headless as a master and the boomer-worker Go binary as its only
worker, both as children of this process, so a Job that runs this ends when
the run ends rather than being stopped from outside. The ordering that makes
that work:

  * locust's -t stops the test and sends a "quit" to the worker.
  * boomer returns from its run loop on that quit and suspends and deletes
    every actor it created. The SIGTERM below is the fallback for a master
    that died without sending one; boomer answers either the same way.
  * this process exits once both have, and its status is the run's.

Adapted from substrate's benchmarking/locust/runner.py, minus the two parts
that do not apply here. The Python user classes are gone, and with them the
choice of load generator: runworkload is implemented in boomer, so boomer
always runs. The result upload is gone as well — the results of a run are the
logs of its pod, so there is no --dest, no JSONL conversion and no CSV.

Upstream's load shapes are not carried over either: -f takes one test file,
because this repo ships no shapes/ directory for the second path to name.
"""

import argparse
import subprocess
import signal
import sys
import threading
import time
from pathlib import Path
from typing import IO

from reports.prometheus import DEFAULT_URL as DEFAULT_PROMETHEUS_URL

# Path inside the locust image to the boomer-worker binary baked in by
# benchmarking/locust/Dockerfile.
BOOMER_BINARY = "/app/boomer-worker"

# Where --test-case looks, copied into the image from benchmarking/tests.yaml.
TESTS_FILE = "/app/tests.yaml"

# Prefix locust's --csv appends _stats.csv, _stats_history.csv, _failures.csv
# and _exceptions.csv to, inside --report-dir so one folder holds everything a
# run produced and the upload collects it in one pass.
LOCUST_CSV_STEM = "locust"

# The case keys that are the run's load rather than the worker's flags. Every
# other key in a case goes to the worker verbatim, so this tuple is the whole
# of what this wrapper claims from a case.
LOAD_KEYS = ("duration", "users", "spawn-rate")

# Case keys that shape the WorkerPool, which run_locust_job.sh deploys before
# the Job starts. Dropped here rather than passed on: the pool is already up by
# the time this runs, and the worker has no such flags — an unknown one is
# fatal to it, so a case carrying these would never reach a first iteration.
HOST_KEYS = ("worker-count", "sandbox-class")

# How long boomer gets to suspend and delete its actors after the run. Longer
# than the worker's own 60s shutdown budget, so the timeout here is reached
# only by a worker that is not going to finish at all.
BOOMER_STOP_TIMEOUT_SECS = 300

# Prometheus scrapes every 10s, so the run's last samples land after the run
# does. Reports wait this long before querying rather than missing the tail.
REPORT_SETTLE_SECS = 15

# Bucket subfolder the reports are uploaded under. Mirrors
# reports.upload.DEFAULT_PREFIX, named here so --help states it without
# importing a package that may be absent.
REPORT_PREFIX = "reports"


def report_names() -> list[str]:
    """Registered report names, or empty when the reports package cannot
    load. Imported lazily so a broken package costs the reports and not the
    run."""
    try:
        import reports

        return reports.names()
    except Exception:
        return []


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-f", required=True, dest="file", help="Locust test file (-f)")
    # A case is the whole run: the load and the worker's flags both, named as
    # one word so the run is reproducible from it.
    p.add_argument(
        "--test-case",
        required=True,
        help=f"Name a case in --tests-file. {', '.join(LOAD_KEYS)} are the "
        f"load; every other key is a worker flag.",
    )
    p.add_argument(
        "--tests-file",
        default=TESTS_FILE,
        help=f"Where --test-case looks (default: {TESTS_FILE}).",
    )
    p.add_argument(
        "--run-id",
        default="",
        help="Names this run in Prometheus. Reports query on it, so without "
        "it they are skipped.",
    )
    p.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="NAME",
        help=f"Report to write when the run ends; repeatable. Known: "
        f"{', '.join(report_names())}, or 'all'.",
    )
    p.add_argument(
        "--report-dir",
        default="/app/reports",
        help="Where --report writes. Pod-local unless a volume is mounted here.",
    )
    p.add_argument(
        "--prometheus-url",
        default=DEFAULT_PROMETHEUS_URL,
        help="Prometheus the reports read the run back from.",
    )
    p.add_argument(
        "--report-bucket",
        default="",
        help="GCS bucket the reports are uploaded to, as gs://<bucket>/"
        "<prefix>/<run-id>/. Empty leaves them on the pod, where the Job's "
        "TTL eventually takes them.",
    )
    p.add_argument(
        "--report-prefix",
        default=REPORT_PREFIX,
        help="Subfolder under --report-bucket.",
    )
    args, extra = p.parse_known_args()
    case = load_test_case(args.tests_file, args.test_case)
    # Split before anything reads either half: what is left after the load
    # comes out is the worker's, whatever it is named.
    args.duration, args.users, args.spawn_rate = take_load(case, args.test_case)
    for key in HOST_KEYS:
        case.pop(key, None)
    args.worker_flags = case
    # The manifest passes --report unconditionally, so an unset one arrives as
    # an empty string rather than not at all.
    args.report = [r for r in args.report if r]
    # Everything unrecognized goes to locust verbatim, which is how a run
    # reaches flags this wrapper has no opinion about (--exit-code-on-error,
    # --stop-timeout, and so on).
    args.locust_extra = extra
    return args


def take_load(case: dict[str, str], name: str) -> tuple[str, int, float]:
    """Remove the load keys from `case` and return them validated.

    The checks are here because nothing downstream makes them: locust reads -u
    and -r as numbers and a wrong one becomes a traceback inside the pod, long
    after the Job was accepted. The duration is left to locust, which knows its
    own spelling of one and rejects anything else at startup.
    """
    for key in ("duration", "users"):
        if key not in case:
            sys.exit(f"Test case {name!r} has no `{key}`; a case states the load it runs at.")

    duration = case.pop("duration")
    try:
        users = int(case.pop("users"))
    except ValueError:
        sys.exit(f"Test case {name!r}: `users` must be a whole number.")
    if users < 1:
        sys.exit(f"Test case {name!r}: `users` must be at least 1, got {users}.")

    # Locust fills in 1 user/sec when -r is absent, which makes the ramp of a
    # large run longer than the run: 100 users would spend 100s climbing, and
    # every sample taken during the climb is at a concurrency the run never
    # reports. Defaulting to the user count puts the whole load on at the first
    # dispatch instead, so the measurement covers one load level.
    raw = case.pop("spawn-rate", None)
    if raw is None:
        return duration, users, float(users)
    try:
        spawn_rate = float(raw)
    except ValueError:
        sys.exit(f"Test case {name!r}: `spawn-rate` must be a number.")
    if spawn_rate <= 0:
        sys.exit(f"Test case {name!r}: `spawn-rate` must be positive, got {spawn_rate}.")
    return duration, users, spawn_rate


def load_test_case(path: str, name: str) -> dict[str, str]:
    """The named case from `path`, or exit saying which names it does hold."""
    try:
        import yaml
    except ModuleNotFoundError:
        sys.exit("--test-case needs PyYAML; add it to locust/requirements.txt.")

    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except OSError as e:
        sys.exit(f"Cannot read {path}: {e}")
    except yaml.YAMLError as e:
        sys.exit(f"{path} is not valid YAML: {e}")

    cases = doc.get("cases")
    if not isinstance(cases, dict):
        sys.exit(f"{path} has no `cases` mapping.")
    if name not in cases:
        sys.exit(f"No test case {name!r} in {path}. Known: {', '.join(sorted(cases)) or '(none)'}")

    case = cases[name] or {}
    if not isinstance(case, dict):
        sys.exit(f"Test case {name!r} in {path} is not a mapping of flags.")
    # str() rather than the YAML type: every one of these is about to be a
    # command-line argument, and a bare 0.05 or 2s parses as float and str
    # respectively for no difference that survives the argv.
    return {str(k): str(v) for k, v in case.items()}


def user_class(test_path: str) -> str:
    """The boomer --user-class that `test_path` names.

    The file name is the class name throughout this repo: deploy.sh accepts a
    --user-class only when tests/NAME.py exists, locust is given that file,
    and NAME is the entry the Go package registered with userclass.Add.
    """
    return Path(test_path).stem


def pump_stream(prefix: str, stream: IO[str]) -> None:
    """Forward each line of `stream` to stdout under a per-source prefix, so
    the two subprocesses stay tellable apart in one stream of pod logs."""
    for line in stream:
        sys.stdout.write(f"[{prefix}] {line.rstrip()}\n")
        sys.stdout.flush()


def log_run_config(args: argparse.Namespace) -> None:
    for line in (
        "==== Run config ====",
        f"  file:         {args.file}",
        f"  user_class:   {user_class(args.file)}",
        f"  duration:     {args.duration}",
        f"  users:        {args.users}",
        f"  spawn_rate:   {args.spawn_rate:g}/s"
        + (" (all at once)" if args.spawn_rate >= args.users else ""),
        f"  test_case:    {args.test_case or '(none)'}",
        f"  worker flags: {' '.join(f'--{k}={v}' for k, v in args.worker_flags.items()) or '(none)'}",
        f"  reports:      {', '.join(args.report) or '(none)'}",
        f"  report dest:  {'gs://' + args.report_bucket + '/' + args.report_prefix if args.report_bucket else args.report_dir + ' (pod-local)'}",
        f"  extra flags:  {' '.join(args.locust_extra) or '(none)'}",
        "====================",
    ):
        print(line, flush=True)


def build_locust_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable, "-m", "locust",
        "--headless",
        "-f", args.file,
        "-t", args.duration,
        "-u", str(args.users),
        # Without this locust defaults to 1 user/sec; see parse_args.
        "-r", f"{args.spawn_rate:g}",
        # Master mode so boomer can connect as a worker on localhost:5557,
        # and --expect-workers so the clock does not start until it has. The
        # seconds before a worker attaches would otherwise be counted as part
        # of the run rather than as part of its setup.
        "--master", "--expect-workers", "1",
        # locust's own stats, at its own precision: it holds response times
        # rounded to 1ms below 100ms, where the worker's Prometheus histogram
        # buckets exponentially and cannot resolve better than the bucket a
        # value lands in. These files are the run's numbers; the Prometheus
        # reports beside them are what survives the pod.
        "--csv", str(Path(args.report_dir) / LOCUST_CSV_STEM),
        # A row per second per request name, rather than one Aggregated row.
        # The user count is recorded in the row with the percentiles, so a
        # ramp needs nothing inferred to say what load a sample was taken at.
        "--csv-full-history",
    ]
    return cmd + list(args.locust_extra)


def build_boomer_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        BOOMER_BINARY,
        "--user-class", user_class(args.file),
    ]
    return cmd + [f"--{name}={value}" for name, value in args.worker_flags.items()]


def stop_boomer(proc: subprocess.Popen) -> int:
    """Shut boomer down and return its exit status.

    Normally it is already on its way out by the time this runs, having been
    told to quit by the master. The signal is for the case where it was not,
    and is safe in either: the worker keeps handling SIGTERM after its run
    loop returns, so one arriving mid-teardown is ignored rather than cutting
    the actor cleanup short.
    """
    print("Stopping boomer...", flush=True)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=BOOMER_STOP_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        print(
            f"Boomer did not exit within {BOOMER_STOP_TIMEOUT_SECS}s; killing. "
            "Actors it created may be left behind.",
            flush=True,
        )
        proc.kill()
        proc.wait()
    print(f"Boomer exited with code {proc.returncode}", flush=True)
    return proc.returncode


def run(args: argparse.Namespace) -> int:
    # locust opens its CSVs at startup, so the directory has to exist before
    # it launches rather than when the reports are written.
    Path(args.report_dir).mkdir(parents=True, exist_ok=True)

    locust_cmd = build_locust_cmd(args)
    print(f"Running: {' '.join(locust_cmd)}", flush=True)
    locust_proc = subprocess.Popen(
        locust_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True,
    )

    boomer_cmd = build_boomer_cmd(args)
    print(f"Running: {' '.join(boomer_cmd)}", flush=True)
    boomer_proc = subprocess.Popen(
        boomer_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True,
    )

    pumps = [
        threading.Thread(target=pump_stream, args=(name, proc.stdout), daemon=True)
        for name, proc in (("locust", locust_proc), ("boomer", boomer_proc))
    ]
    for t in pumps:
        t.start()

    locust_exit = locust_proc.wait()
    print(f"Locust exited with code {locust_exit}", flush=True)
    boomer_exit = stop_boomer(boomer_proc)

    for t in pumps:
        t.join(timeout=5)

    # Both statuses matter, because the exit code is the only result this
    # carries: locust reports whether the run met its own bar, and a worker
    # that died took the load generator with it whatever locust concluded.
    # Locust's own is preferred when both failed, as the more specific of the
    # two. --exit-code-on-error=0 is the way to say that failed requests are
    # an expected part of a run.
    if locust_exit != 0:
        return locust_exit
    return boomer_exit


def locust_csvs(args: argparse.Namespace) -> list[Path]:
    """The CSVs locust wrote for this run, already complete by the time it
    exited. Collected by glob rather than by name so a file locust adds in a
    later version is uploaded without this list being updated."""
    return sorted(Path(args.report_dir).glob(f"{LOCUST_CSV_STEM}_*.csv"))


def write_reports(args: argparse.Namespace, started: float, ended: float) -> None:
    """Write the selected reports and upload them with locust's own CSVs.
    Never raises: a report is a by-product of the run, so a broken one must
    not change what the run concluded."""
    if not args.run_id:
        print("reports: --run-id is unset, so there is nothing to query on; skipping", flush=True)
        return

    # Locust's files are on disk already, whatever the Prometheus reports do.
    collected = locust_csvs(args)
    for path in collected:
        print(f"reports: locust wrote {path} ({path.stat().st_size} bytes)", flush=True)

    if args.report:
        collected += generate_reports(args, started)

    if collected and args.report_bucket:
        upload_reports(collected, args)


def generate_reports(args: argparse.Namespace, started: float) -> list[Path]:
    """The Prometheus-derived reports, or an empty list if they cannot be
    produced. Each failure is named; none of them is fatal."""
    print(f"Waiting {REPORT_SETTLE_SECS}s for the run's last scrape...", flush=True)
    time.sleep(REPORT_SETTLE_SECS)

    try:
        import reports

        ctx = reports.RunContext(
            run_id=args.run_id,
            users=args.users,
            spawn_rate=args.spawn_rate,
            duration=args.duration,
            workload=args.worker_flags.get("workload-name", ""),
            template=args.worker_flags.get("workload-template", ""),
            user_class=user_class(args.file),
            started=started,
            ended=time.time(),
            prometheus_url=args.prometheus_url,
        )
        results = reports.generate(args.report, ctx, Path(args.report_dir))
    except Exception as exc:
        print(f"reports: skipped ({type(exc).__name__}: {exc})", flush=True)
        return []

    written = []
    for name, path, error in results:
        if error:
            print(f"reports: {name} failed: {error}", flush=True)
        else:
            written.append(path)
            print(f"reports: wrote {path} ({path.stat().st_size} bytes)", flush=True)
    return written


def upload_reports(paths: list[Path], args: argparse.Namespace) -> None:
    try:
        import reports

        results = reports.upload(
            paths, args.report_bucket, args.run_id, Path(args.report_dir), args.report_prefix
        )
    except ImportError as exc:
        print(f"reports: upload skipped ({exc})", flush=True)
        return
    except reports.UploadError as exc:
        print(f"reports: upload skipped ({exc})", flush=True)
        return
    for path, uri, error in results:
        if error:
            print(f"reports: upload of {path.name} failed: {error}", flush=True)
        else:
            print(f"reports: uploaded {uri}", flush=True)


def main() -> None:
    args = parse_args()
    log_run_config(args)
    started = time.time()
    status = run(args)
    write_reports(args, started, time.time())
    sys.exit(status)


if __name__ == "__main__":
    main()
