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
    load. Imported lazily so a missing matplotlib costs the reports and not
    the run."""
    try:
        import reports

        return reports.names()
    except Exception:
        return []


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-f", required=True, dest="file", help="Locust test file (-f)")
    p.add_argument("-t", required=True, dest="duration", help="Run duration (-t)")
    p.add_argument(
        "-u", required=True, type=int, dest="users", help="Number of users (-u)"
    )
    # Locust fills in 1 user/sec when -r is absent, which makes the ramp of a
    # large run longer than the run: -u 100 would spend 100s climbing, and
    # every sample taken during the climb is at a concurrency the run never
    # reports. Defaulting to the user count puts the whole load on at the
    # first dispatch instead, so the measurement covers one load level.
    p.add_argument(
        "-r",
        "--spawn-rate",
        type=float,
        default=None,
        dest="spawn_rate",
        help="Users per second (-r). Unset starts every user at once, which "
        "is what a fixed-load measurement wants; give a smaller number to "
        "ramp in.",
    )
    # The two runworkload settings, which have no locust counterpart: they are
    # the worker's flags and are forwarded to it rather than to locust. Empty
    # is meaningful for both and is the default, so that what a value is left
    # unset selects stays a question for the worker to answer.
    p.add_argument(
        "--workload-name",
        default="",
        help="runworkload only: the workload a RunWorkloadUser drives. "
        "Unset selects the worker's own default.",
    )
    p.add_argument(
        "--workload-template",
        default="",
        help="runworkload only: the ActorTemplate the workload is measured on. "
        "Unset selects the workload's own.",
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
    # Resolved here rather than at the flag, because the default is another
    # argument's value and argparse cannot express that.
    if args.spawn_rate is None:
        args.spawn_rate = float(args.users)
    # The manifest passes --report unconditionally, so an unset one arrives as
    # an empty string rather than not at all.
    args.report = [r for r in args.report if r]
    # Everything unrecognized goes to locust verbatim, which is how a run
    # reaches flags this wrapper has no opinion about (--exit-code-on-error,
    # --stop-timeout, and so on).
    args.locust_extra = extra
    return args


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
        f"  workload:     {args.workload_name or '(the worker default)'}",
        f"  template:     {args.workload_template or '(the workload default)'}",
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
    ]
    return cmd + list(args.locust_extra)


def build_boomer_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        BOOMER_BINARY,
        "--user-class", user_class(args.file),
    ]
    # Passed only when set: the flags read an empty value as unset, and
    # leaving them off entirely says the same thing more plainly in the log.
    if args.workload_name:
        cmd += ["--workload-name", args.workload_name]
    if args.workload_template:
        cmd += ["--workload-template", args.workload_template]
    return cmd


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


def write_reports(args: argparse.Namespace, started: float, ended: float) -> None:
    """Write the selected reports. Never raises: a report is a by-product of
    the run, so a broken one must not change what the run concluded."""
    if not args.report:
        return
    if not args.run_id:
        print("reports: --run-id is unset, so there is nothing to query on; skipping", flush=True)
        return

    print(f"Waiting {REPORT_SETTLE_SECS}s for the run's last scrape...", flush=True)
    time.sleep(REPORT_SETTLE_SECS)

    try:
        import reports

        ctx = reports.RunContext(
            run_id=args.run_id,
            users=args.users,
            spawn_rate=args.spawn_rate,
            duration=args.duration,
            workload=args.workload_name,
            template=args.workload_template,
            user_class=user_class(args.file),
            started=started,
            ended=time.time(),
            prometheus_url=args.prometheus_url,
        )
        results = reports.generate(args.report, ctx, Path(args.report_dir))
    except Exception as exc:
        print(f"reports: skipped ({type(exc).__name__}: {exc})", flush=True)
        return

    written = []
    for name, path, error in results:
        if error:
            print(f"reports: {name} failed: {error}", flush=True)
        else:
            written.append(path)
            print(f"reports: wrote {path} ({path.stat().st_size} bytes)", flush=True)

    if written and args.report_bucket:
        upload_reports(written, args, reports)


def upload_reports(paths: list[Path], args: argparse.Namespace, reports) -> None:
    try:
        results = reports.upload(
            paths, args.report_bucket, args.run_id, args.report_prefix
        )
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
