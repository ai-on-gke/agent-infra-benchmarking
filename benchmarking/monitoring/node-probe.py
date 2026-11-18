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

"""Logs, once a second as a JSON line, what the node's kernel is waiting on:
pressure stall info, dirty and writeback memory, the tasks in uninterruptible
sleep grouped by their kernel stacks, and the disks' in-flight requests, with
each ext4 journal's stats every 15 s, and apart from those, how long reading
the host's mount table takes. Runs in a privileged pod with the host's PID
namespace, which is what lets it read every task's stack."""

import collections
import glob
import itertools
import json
import os
import re
import threading
import time

VMSTAT = {
    "nr_dirty", "nr_writeback", "nr_dirty_threshold", "nr_dirty_background_threshold",
    "allocstall_normal", "pgscan_direct", "pgsteal_direct",
}  # fmt: skip
STACK_FRAMES = 6
TOP_STACKS = 12


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def pressure(kind):
    out = {}
    for line in read(f"/proc/pressure/{kind}").splitlines():
        scope, *fields = line.split()
        out[scope] = {k: float(v) for k, v in (field.split("=") for field in fields)}
    return out


def counters(path, keys):
    out = {}
    for line in read(path).splitlines():
        parts = line.replace(":", " ").split()
        if parts and parts[0] in keys:
            out[parts[0]] = int(parts[1])
    return out


def blocked_tasks():
    stacks = collections.Counter()
    for stat in glob.glob("/proc/[0-9]*/task/[0-9]*/stat"):
        text = read(stat)
        end = text.rfind(")")
        if end < 0 or text[end + 2 : end + 3] != "D":
            continue
        comm = text[text.find("(") + 1 : end]
        task = os.path.dirname(stat)
        frames = [line.split()[1].split("+")[0] for line in read(f"{task}/stack").splitlines() if len(line.split()) > 1]
        where = " < ".join(frames[:STACK_FRAMES]) if frames else "wchan=" + read(f"{task}/wchan")
        stacks[f"{comm} | {where}"] += 1
    return stacks


def in_flight():
    disks = {}
    for line in read("/proc/diskstats").splitlines():
        fields = line.split()
        if re.fullmatch(r"nvme\d+n\d+|sd[a-z]+", fields[2]):
            disks[fields[2]] = int(fields[11])
    return disks


def time_mount_table():
    # A thread of its own, as reading the mount table waits on the lock that
    # mounts and unmounts hold, which would stall the samples too.
    while True:
        start = time.time()
        count = read("/proc/1/mountinfo").count("\n")
        took = time.time() - start
        print(json.dumps({"probe": "mounts", "t": round(start, 3), "count": count, "took": round(took, 3)}), flush=True)
        time.sleep(max(0.0, 1 - took))


def main():
    settings = {os.path.basename(p): read(p).strip() for p in glob.glob("/proc/sys/vm/dirty*")}
    print(json.dumps({"probe": "vm-settings", **settings}), flush=True)
    threading.Thread(target=time_mount_table, daemon=True).start()
    for n in itertools.count():
        start = time.time()
        blocked = blocked_tasks()
        sample = {
            "probe": "sample",
            "t": round(start, 3),
            "io": pressure("io"),
            "memory": pressure("memory"),
            "cpu": pressure("cpu"),
            "meminfo": counters("/proc/meminfo", {"Dirty", "Writeback", "MemFree", "Cached"}),
            "vmstat": counters("/proc/vmstat", VMSTAT),
            "in_flight": in_flight(),
            "blocked_total": sum(blocked.values()),
            "blocked": dict(blocked.most_common(TOP_STACKS)),
        }
        sample["took"] = round(time.time() - start, 3)
        print(json.dumps(sample, separators=(",", ":")), flush=True)
        if n % 15 == 0:
            for info in glob.glob("/proc/fs/jbd2/*/info"):
                print(json.dumps({"probe": "jbd2", "t": round(start, 3), "dev": info.split("/")[4], "info": read(info)}), flush=True)
        time.sleep(max(0.0, 1 - (time.time() - start)))


if __name__ == "__main__":
    main()
