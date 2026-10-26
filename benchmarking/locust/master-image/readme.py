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

"""README.md for a run's folder: run.json summarized for a reader, and what
each file beside it holds. render_summary does the same for a case's repeats,
for the scenario-runner, which loads this file from here.

Standard library only, for that reason. Anything run.json lacks is shown as
"-" rather than failing the README.
"""

import datetime
import signal

UNKNOWN = "-"

# Every file a run's folder can hold, in the order a reader wants them.
FILES = (
    (
        "locust_stats_steady.csv",
        "the run's latency percentiles: locust's own totals per request name over the steady-state "
        "window only. Failed requests included, rounded to 1 ms below 100 ms, 10 ms below 1 s, "
        "100 ms below 10 s and 1 s above.",
    ),
    ("locust_stats.csv", "the same over the whole run, including the ramp, the warm-up and the teardown."),
    ("locust_stats_history.csv", "the same, one row per second per request name, with the user count at that second."),
    ("locust_failures.csv", "failed requests, grouped by error."),
    ("locust_exceptions.csv", "errors raised inside the load generator."),
    ("custom_stats_history.csv", "active and idle actors over the run, from Prometheus."),
    ("cluster_vitals_history.csv", "CPU and memory of every node over the run; worker nodes have Pool=worker."),
    ("locust_steady_window.json", "the steady-state window's bounds, as the master measured them."),
    (
        "run.json",
        "everything above in full: the resolved case, the cluster and its nodes, the WorkerPool, "
        "ActorTemplate and SandboxConfig, every image by digest, and the commits.",
    ),
    (
        "cleanup.json",
        "written by the scenario-runner after the run: the actors it found left and deleted, and any "
        "still there a minute later.",
    ),
)


def render(info: dict, stats: list[dict] | None = None, stats_file: str | None = None) -> str:
    """stats are the rows of the run's stats CSV, stats_file's, for its
    results table."""
    sections = [
        _header(info),
        _results(info, stats or [], stats_file),
        _case(info),
        _hardware(info),
        _workers(info),
        _versions(info),
        _files(),
    ]
    return "\n\n".join(s for s in sections if s) + "\n"


# The summary's results table: (stat in the summary CSV, column heading).
SUMMARY_STATS = (
    ("Requests/s", "Requests/s"),
    ("Average Response Time", "Average"),
    ("50%", "p50"),
    ("90%", "p90"),
    ("99%", "p99"),
    ("99.9%", "p99.9"),
    ("Max Response Time", "Max"),
)


def render_summary(case: str, jobs: list[tuple[str, dict | None, str | None]], rows: list[list[str]]) -> str:
    """The README of a case's repeats. `jobs` is each repeat's folder name,
    run.json and reason it was left out of the summary, if any; `rows` the
    summary CSV's rows."""
    infos = [(name, info) for name, info, _ in jobs if info]
    names = [(info.get("cluster") or {}).get("name") if isinstance(info.get("cluster"), dict) else None
             for _, info in infos]  # fmt: skip
    clusters = None
    if all(names) and len(set(names)) > 1:
        clusters = "clusters " + ", ".join(f"`{name}`" for name in dict.fromkeys(names)) + ", one per repeat,"
    sections = [
        _summary_header(case, jobs),
        _summary_results(case, jobs, rows),
        _same_across(infos, _case),
        _same_across(infos, _hardware, lambda info: _hardware(info, clusters)),
        _same_across(infos, _workers, lambda info: _workers(info, golden=False)),
        _same_across(infos, _versions),
        _summary_files(case, jobs),
    ]
    return "\n\n".join(s for s in sections if s) + "\n"


def _summary_header(case: str, jobs) -> str:
    infos = [info for _, info, _ in jobs if info]
    run_id = next((i["run_id"] for i in infos if i.get("run_id")), UNKNOWN)
    used = sum(1 for _, _, reason in jobs if not reason)
    shared = any(_repeats(i).get("cluster") == "shared" for i in infos)
    lines = [f"# {case}, {len(jobs)} repeats{' on one cluster' if shared else ''}, run {run_id}", ""]
    lines.append(f"- **Summarized:** {used} of {len(jobs)} repeats, the ones that passed")

    counted = [i["failures"] for i in infos if isinstance(i.get("failures"), dict) and "cycles" in i["failures"]]
    if counted:
        total = {key: sum(f[key] for f in counted) for key in ("cycles", "failed_cycles", "failed_starts")}
        attempted = total["cycles"] + total["failed_starts"]
        total["ratio"] = (total["failed_cycles"] + total["failed_starts"]) / attempted if attempted else None
        max_ratio = next((i.get("max_failure_ratio") for i in infos), None)
        lines.append(f"- **Failed cycles, all repeats:** {_failures(total, max_ratio)}")
    lines.append("")

    rows = []
    for name, info, reason in jobs:
        if not info:
            rows.append([name, reason, UNKNOWN, UNKNOWN, UNKNOWN])
            continue
        steady = info.get("steady_window") if isinstance(info.get("steady_window"), dict) else {}
        rows.append([
            name,
            _status(info.get("status")) + (f"; left out, {reason}" if reason and info.get("status") == 0 else ""),
            _failures(info.get("failures"), None),
            _window(info.get("started"), info.get("ended")),
            _window(steady.get("start"), steady.get("end")) if steady else "none recorded",
        ])
    lines += _table(["Repeat", "Result", "Failed cycles", "Window", "Steady-state window"], rows)
    return "\n".join(lines)


def _summary_results(case: str, jobs, rows: list[list[str]]) -> str:
    lines = ["## Results", ""]
    used = sum(1 for _, _, reason in jobs if not reason)
    if not rows:
        lines.append("No repeat passed with steady-state stats, so there is nothing to summarize.")
        return "\n".join(lines)
    lines.append(
        "The median over the repeats that passed, with the lowest and highest in brackets, of each one's "
        f"`locust_stats_steady.csv`; latencies in ms. `{case}-summary.csv` has every stat."
    )
    lines.append("")

    stats: dict[str, dict[str, list[str]]] = {}
    for _, name, stat, count, low, median, high in rows:
        stats.setdefault(name, {})[stat] = [count, low, median, high]

    def cell(value) -> str:
        if not value:
            return UNKNOWN
        count, low, median, high = value
        text = median if low == high else f"{median} ({low}-{high})"
        return text if int(count) == used else f"{text}, in {count}"

    table = [[name, *(cell(by_stat.get(stat)) for stat, _ in SUMMARY_STATS)] for name, by_stat in stats.items()]
    lines += _table(["Request", *(heading for _, heading in SUMMARY_STATS)], table)
    return "\n".join(lines)


def _same_across(infos: list[tuple[str, dict]], section, shared=None) -> str:
    """`section` as each repeat's README has it: once if it reads the same in
    all of them, otherwise once for each version, naming the repeats it is.
    `shared` is the section without what is each repeat's own, such as its
    cluster's name, shown once instead if that reads the same in all."""
    if shared:
        texts = {shared(info) for _, info in infos}
        if len(texts) == 1:
            return texts.pop()
    versions: dict[str, list[str]] = {}
    for name, info in infos:
        versions.setdefault(section(info), []).append(name)
    if len(versions) <= 1:
        return next(iter(versions), "")
    heading = next(text.partition("\n")[0] for text in versions if text)
    lines = [heading, "", "Not the same in every repeat."]
    for text, names in versions.items():
        body = text.partition("\n\n")[2] if text else "Not recorded."
        lines += ["", f"**{', '.join(names)}:**", "", body]
    return "\n".join(lines)


def _summary_files(case: str, jobs) -> str:
    return "\n".join([
        "## Files",
        "",
        f"- `{case}-summary.csv`: the min, median and max of every stat in the `locust_stats_steady.csv` "
        "of the repeats that passed, with how many of them it was found in.",
        *(f"- `{name}/`: the repeat's own files, and its README." for name, _, _ in jobs),
    ])


def _locust(case: dict) -> dict:
    """The case's `locust`, the keys of its type."""
    return case.get("locust") if isinstance(case.get("locust"), dict) else {}


def _repeats(info: dict) -> dict:
    """The case's `repeats` as run.json records it, from a run before it was a
    mapping the count alone."""
    case = info.get("case") if isinstance(info.get("case"), dict) else {}
    repeats = case.get("repeats")
    return repeats if isinstance(repeats, dict) else {"count": repeats}


def _header(info: dict) -> str:
    title = info.get("test_case") or UNKNOWN
    if info.get("warmup"):
        title += f", warm-up {info['warmup']}, not one of its repeats"
    elif info.get("repeat"):
        title += f", repeat {info['repeat']} of {_repeats(info).get('count') or UNKNOWN}"
    lines = [f"# {title}, run {info.get('run_id') or UNKNOWN}", ""]
    lines.append(f"- **Result:** {_status(info.get('status'))}")
    failures = info.get("failures") if isinstance(info.get("failures"), dict) else {}
    lines.append(f"- **Failed cycles:** {_failures(failures or None, info.get('max_failure_ratio'))}")
    if isinstance(failures.get("steady"), dict):
        lines.append(f"- **Failed cycles, steady state:** {_failures(failures['steady'], None)}")
    if failures.get("exceptions"):
        lines.append(f"- **Load generator exceptions:** {failures['exceptions']}")
    lines.append(f"- **Window:** {_window(info.get('started'), info.get('ended'))}")
    lines.append(f"- **Steady-state window:** {_steady_window(info.get('steady_window'), info.get('warm_up'))}")
    errors = info.get("run_info_errors")
    if isinstance(errors, dict) and errors:
        lines.append(f"- **Metadata missing:** {', '.join(f'{k} ({v})' for k, v in errors.items())}")
    return "\n".join(lines)


# The per-run results table: the request names in a cycle's order.
RESULT_ROWS = ("Resume", "ExecutionTime", "TTFI", "Suspend", "CycleTime")
# The worker's row for an actor after a failed cycle: a success kept it, a
# failure replaced it.
RECOVER_ROW = "Recover"
# Workloads whose instruction takes long enough to dominate TTFI and CycleTime,
# so that only Resume and Suspend say much about the sandbox itself.
SLOW_INSTRUCTION_WORKLOADS = ("openclaw",)


def _results(info: dict, stats: list[dict], stats_file: str | None) -> str:
    rows = {row.get("Name"): row for row in stats}
    if not any(name in rows for name in RESULT_ROWS):
        return ""
    lines = ["## Results", "", f"From `{stats_file or UNKNOWN}`; latencies in ms.", ""]
    table = []
    for name in RESULT_ROWS:
        row = rows.get(name)
        if row:
            table.append([f"`{name}`", row.get("Request Count", UNKNOWN), row.get("Failure Count", UNKNOWN),
                          *(_ms(row.get(stat)) for stat in ("50%", "90%", "99%", "Max Response Time"))])  # fmt: skip
    lines += _table(["Request", "Count", "Failed", "p50", "p90", "p99", "Max"], table)
    recovered = rows.get(RECOVER_ROW)
    if recovered:
        try:
            replaced = int(recovered.get("Failure Count"))
            kept = int(recovered.get("Request Count")) - replaced
            lines += ["", f"After a failed cycle, {kept:,} actor(s) settled and were kept, {replaced:,} replaced"
                          f" (`{RECOVER_ROW}`; why in `locust_failures.csv`)."]  # fmt: skip
        except (TypeError, ValueError):
            pass
    case = _locust(info.get("case") if isinstance(info.get("case"), dict) else {})
    worker = case.get("worker") if isinstance(case.get("worker"), dict) else {}
    workload = worker.get("workload-name") or worker.get("workload-template")
    if workload in SLOW_INSTRUCTION_WORKLOADS:
        lines += [
            "",
            f"`{workload}`'s instruction is a whole agent turn, which `ExecutionTime` times on its own and which"
            " makes up most of `TTFI` and `CycleTime`. Compare sandboxes on `Resume` and `Suspend`.",
        ]
    return "\n".join(lines)


def _ms(value) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return UNKNOWN


def _case(info: dict) -> str:
    case = info.get("case")
    lines = [f"## Test case `{info.get('test_case') or UNKNOWN}`", ""]
    if not isinstance(case, dict) or not case:
        lines.append("The resolved case was not recorded.")
        return "\n".join(lines)
    locust = _locust(case)
    master = locust.get("master") if isinstance(locust.get("master"), dict) else {}
    worker = locust.get("worker") if isinstance(locust.get("worker"), dict) else {}
    keys = [[k, v] for k, v in case.items() if k not in ("locust", "infra")]
    keys += [[k, v] for k, v in locust.items() if k not in ("master", "worker", "infra")]
    users = master.get("users")
    workers = (locust.get("workers") or {}).get("count")
    if isinstance(users, (int, float)) and isinstance(workers, (int, float)) and workers > 0:
        keys.append(["density", f"{users / workers:g}x ({users} actors / {workers} workers)"])
    lines += _table(["Key", "Value"], keys)

    users, rate = master.get("users"), master.get("spawn-rate")
    if isinstance(users, (int, float)) and isinstance(rate, (int, float)) and rate > 0:
        lines.append("")
        if rate >= users:
            lines.append(f"All {users} users start at once.")
        else:
            lines.append(f"The {users} users start over the first {users / rate:g} s, at {rate:g}/s.")
    for side, flags in (("Master", master), ("Worker", worker)):
        if flags:
            lines.append("")
            lines.append(f"{side} flags as run: " + " ".join(f"`--{k}={v}`" for k, v in flags.items()))
    return "\n".join(lines)


def _hardware(info: dict, clusters: str | None = None) -> str:
    """clusters, if given, names the clusters in place of the run's own."""
    cluster = info.get("cluster") if isinstance(info.get("cluster"), dict) else {}
    pools = cluster.get("node_pools") if isinstance(cluster.get("node_pools"), dict) else {}
    nodes = info.get("nodes") if isinstance(info.get("nodes"), dict) else {}

    lines = ["## Hardware", ""]
    if not (cluster or nodes):
        lines.append("Neither the cluster's configuration nor its nodes were recorded.")
        return "\n".join(lines)
    if cluster:
        named = clusters or f"cluster `{cluster.get('name') or UNKNOWN}`"
        lines.append(
            f"GKE {named} in {cluster.get('location') or UNKNOWN}: "
            f"control plane {cluster.get('control_plane_version') or UNKNOWN}, "
            f"release channel {cluster.get('release_channel') or UNKNOWN}."
        )
    else:
        lines.append("The cluster's GKE configuration was not recorded; the columns below come from the nodes alone.")
    lines.append("")

    rows = []
    for name in sorted(set(pools) | set(nodes)):
        pool = pools.get(name) or {}
        live = nodes.get(name) or {}
        disk_type = pool.get("disk_type") or _joined(live.get("boot_disks"))
        disk_size = pool.get("disk_size_gb")
        rows.append([
            name,
            f"{live['ready']}/{live['count']}" if "count" in live else UNKNOWN,
            pool.get("machine_type") or _joined(live.get("machine_types")),
            _joined(live.get("cpu")),
            _joined(live.get("memory_gib"), " GiB"),
            f"{disk_type}, {disk_size} GB" if disk_size else disk_type,
            _local_ssd(pool),
            _joined(live.get("kubelet_versions")) if live.get("kubelet_versions") else pool.get("version") or UNKNOWN,
            _on_off(pool.get("auto_upgrade")),
            _autoscaling(pool.get("autoscaling")) if pool else UNKNOWN,
            ", ".join(pool.get("locations") or live.get("zones") or []) or UNKNOWN,
        ])
    lines += _table(
        ["Node pool", "Nodes ready", "Machine", "vCPU", "Memory", "Boot disk", "Local SSD",
         "Node version", "Auto-upgrade", "Autoscaling", "Zones"],
        rows,
    )
    return "\n".join(lines)


def _workers(info: dict, golden: bool = True) -> str:
    """golden: with when the golden snapshot was taken."""
    pool = info.get("worker_pool") if isinstance(info.get("worker_pool"), dict) else {}
    templates = info.get("actor_templates") if isinstance(info.get("actor_templates"), dict) else {}
    configs = info.get("sandbox_configs") if isinstance(info.get("sandbox_configs"), dict) else {}
    if not (pool or templates):
        return ""

    lines = ["## Workers and actors", ""]
    if pool:
        spec, status = pool.get("spec") or {}, pool.get("status") or {}
        ready = status.get('readyReplicas', 0)
        replicas = spec.get('replicas', UNKNOWN)
        lines.append(
            f"WorkerPool: {ready}/{replicas} workers ready, "
            f"sandbox class {spec.get('sandboxClass') or UNKNOWN}."
        )
        case = _locust(info.get("case") or {})
        users = (case.get("master") or {}).get("users")
        workers = spec.get("replicas") or (case.get("workers") or {}).get("count")
        if isinstance(users, (int, float)) and isinstance(workers, (int, float)) and workers > 0:
            density = users / workers
            sandboxed = ((info.get("nodes") or {}).get("sandboxed") or {}).get("count")
            node_desc = f", {users / sandboxed:g} actors/node" if sandboxed else ""
            lines.append(f"Density: {density:g}x ({users} actors / {workers} workers{node_desc}).")
    for name, tmpl in templates.items():
        if not isinstance(tmpl, dict):
            continue
        sandbox = tmpl.get("sandboxConfig") or {}
        config_name = sandbox.get("configName")
        parts = [f"sandbox config `{config_name or UNKNOWN}`"]
        runtime = _sandbox_assets(configs.get(config_name), info.get("bucket"))
        if runtime:
            parts.append(f"runtime {runtime}")
        memory = [
            r.get("quantity") for r in (tmpl.get("resources") or {}).get("limits") or [] if r.get("name") == "memory"
        ]
        parts.append(f"memory limit {memory[0] if memory else 'unset'}")
        # The scope of every snapshot of an actor, on pause and on suspend.
        snapshots = tmpl.get("snapshotConfig") or {}
        if snapshots.get("onCommit"):
            parts.append(f"snapshot scope {snapshots['onCommit'].removeprefix('SNAPSHOT_CONTENT_SCOPE_')}")
        taken = ((tmpl.get("status") or {}).get("goldenSnapshotStatus") or {}).get("takeGoldenSnapshotAt")
        if golden and taken:
            parts.append(f"golden snapshot taken {taken}")
        lines.append("")
        lines.append(f"ActorTemplate `{name}`: " + ", ".join(parts) + ".")
    return "\n".join(lines)


def _versions(info: dict) -> str:
    rows = []
    commit = info.get("commit")
    if commit:
        rows.append(["Repository commit", f"`{commit}`" + (" (uncommitted changes)" if info.get("commit_dirty") else "")])
    if info.get("substrate_commit"):
        rows.append(["Substrate commit", f"`{info['substrate_commit']}`"])
    if info.get("substrate_version"):
        rows.append(["Substrate build version", f"`{info['substrate_version']}`"])
    for name, image in (info.get("images") or {}).items():
        rows.append([f"Image `{name}`", f"`{image}`"])

    for name, tmpl in (info.get("actor_templates") or {}).items():
        if isinstance(tmpl, dict):
            for c in tmpl.get("containers") or []:
                rows.append([f"ActorTemplate `{name}` / {c.get('name')}", f"`{c.get('image')}`"])
    for name, dep in (info.get("deployments") or {}).items():
        if isinstance(dep, dict):
            for c in dep.get("containers") or []:
                rows.append([f"Deployment `{name}` / {c.get('name')} x{dep.get('replicas', UNKNOWN)}", f"`{c.get('image')}`"])
    for workload, containers in (info.get("control_plane_images") or {}).items():
        if isinstance(containers, dict):
            for container, image in containers.items():
                rows.append([f"Control plane `{workload}` / {container}", _ran(image)])

    tools = info.get("tools") if isinstance(info.get("tools"), dict) else {}
    if any(tools.values()):
        rows.append(["Tools", ", ".join(f"{tool} {version}" for tool, version in tools.items() if version)])

    if not rows:
        return ""
    return "\n".join(["## Versions and images", "", *_table(["Component", "Commit or image"], rows)])


def _ran(image) -> str:
    """A pod image as it ran: its name at each digest the nodes resolved it to."""
    if not isinstance(image, dict):
        return UNKNOWN
    ref = str(image.get("image") or "")
    digests = image.get("digests") or []
    # A bare image ID, which the status shows for an image named by digest, names nothing.
    if not ref or ref.startswith("sha256:"):
        return ", ".join(f"`{digest}`" for digest in digests) or UNKNOWN
    # Only the last path segment can carry a tag, which leaves a registry port alone.
    head, _, last = ref.split("@", 1)[0].rpartition("/")
    name = f"{head}/{last.split(':', 1)[0]}" if head else last.split(":", 1)[0]
    return ", ".join(f"`{name}@{digest}`" for digest in digests) or f"`{ref}`"


def _files() -> str:
    lines = ["## Files", ""]
    lines += [f"- `{name}`: {what}" for name, what in FILES]
    return "\n".join(lines)


def _status(status) -> str:
    if not isinstance(status, int):
        return UNKNOWN
    if status == 0:
        return "passed (exit 0)"
    if status > 128:
        try:
            return f"stopped by {signal.Signals(status - 128).name} (exit {status})"
        except ValueError:
            pass
    return f"failed (exit {status})"


def _failures(failures, max_ratio) -> str:
    """Failed cycles, an actor that could not be started counting as one, as
    max-failure-ratio judges them."""
    if not isinstance(failures, dict) or "cycles" not in failures:
        return UNKNOWN
    starts = failures.get("failed_starts", 0)
    attempted = failures["cycles"] + starts
    if not attempted or failures.get("ratio") is None:
        return "none; the run completed no cycle"
    text = f"{failures['failed_cycles'] + starts:,} of {attempted:,} ({failures['ratio']:.3%})"
    if starts:
        text += f", {starts:,} of them actors not started"
    if isinstance(max_ratio, (int, float)):
        text += f", allowed {max_ratio * 100:g}%"
    return text


def _window(started, ended) -> str:
    try:
        start = datetime.datetime.fromisoformat(started)
        end = datetime.datetime.fromisoformat(ended)
    except (TypeError, ValueError):
        return UNKNOWN
    seconds = int((end - start).total_seconds())
    return (
        f"{start:%Y-%m-%d %H:%M:%S} to {end:%H:%M:%S} UTC "
        f"({seconds // 60}m {seconds % 60}s)"
    )


def _steady_window(window, warm_up) -> str:
    if not isinstance(window, dict):
        return "none recorded"
    return f"{_window(window.get('start'), window.get('end'))}, from {warm_up or UNKNOWN} after the last user started"


def _sandbox_assets(config, bucket: str | None) -> str:
    """The amd64 runtime the SandboxConfig installs, which is what the nodes
    run: each asset by its sha256, and where from unless that is the run's own
    bucket, which differs from run to run."""
    if not isinstance(config, dict):
        return ""
    # New sandboxes boot with the version defaultVersion names.
    versions = [v for v in config.get("versions") or [] if isinstance(v, dict)]
    config = next((v for v in versions if v.get("name") == config.get("defaultVersion")), config)
    parts = []
    for name, asset in sorted(((config.get("assets") or {}).get("amd64") or {}).items()):
        if not isinstance(asset, dict):
            continue
        url = asset.get("url") or ""
        about = [f"sha256 `{asset['sha256']}`"] if asset.get("sha256") else []
        if url and not (bucket and url.startswith(f"gs://{bucket}/")):
            about.append(f"from `{url}`")
        parts.append(f"`{name}`" + (f" ({', '.join(about)})" if about else ""))
    return ", ".join(parts)


def _local_ssd(pool: dict) -> str:
    if not pool:
        return UNKNOWN
    ephemeral = pool.get("ephemeral_local_ssd_count") or 0
    raw = pool.get("local_ssd_count") or 0
    if ephemeral:
        return f"{ephemeral} (ephemeral storage)"
    return str(raw) if raw else "none"


def _autoscaling(scaling) -> str:
    if not isinstance(scaling, dict):
        return "off"
    if scaling.get("min") == scaling.get("max"):
        return f"fixed at {scaling.get('min')} per zone"
    return f"{scaling.get('min')}-{scaling.get('max')} per zone"


def _on_off(value) -> str:
    if value is None:
        return UNKNOWN
    return "on" if value else "off"


def _joined(values, suffix: str = "") -> str:
    if not values:
        return UNKNOWN
    return ", ".join(f"{v}{suffix}" for v in values)


def _table(header: list, rows: list) -> list[str]:
    def cell(v) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(cell(h) for h in header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return out
