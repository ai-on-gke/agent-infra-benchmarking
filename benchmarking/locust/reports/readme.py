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
each file beside it holds.

Rendered from run.json's contents rather than from Prometheus, so it is not a
registered report: it is written whichever reports were selected. Anything
run.json lacks is shown as "—" rather than failing the README.
"""

import datetime
import signal

UNKNOWN = "—"

# Every file a run's folder can hold, in the order a reader wants them.
FILES = (
    ("locust_stats.csv", "locust's own totals per request name, over the whole run including the ramp."),
    ("locust_stats_history.csv", "the same, one row per second per request name, with the user count at that second."),
    ("locust_failures.csv", "failed requests, grouped by error."),
    ("locust_exceptions.csv", "errors raised inside the load generator."),
    (
        "stats.csv",
        "the totals rebuilt from Prometheus. Its percentiles are estimated from histogram buckets "
        "and cover successful requests only.",
    ),
    ("custom_stats_history.csv", "active and idle actors over the run, from Prometheus."),
    ("cluster_vitals_history.csv", "CPU and memory of every node over the run; worker nodes have Pool=worker."),
    (
        "run.json",
        "everything above in full: the resolved case, the cluster and its nodes, the WorkerPool, "
        "ActorTemplate and SandboxConfig, every image by digest, and the commits.",
    ),
)


def render(info: dict) -> str:
    sections = [
        _header(info),
        _case(info),
        _hardware(info),
        _workers(info),
        _versions(info),
        _files(),
    ]
    return "\n\n".join(s for s in sections if s) + "\n"


def _header(info: dict) -> str:
    lines = [f"# {info.get('test_case') or UNKNOWN} — run {info.get('run_id') or UNKNOWN}", ""]
    lines.append(f"- **Result:** {_status(info.get('status'))}")
    lines.append(f"- **Failed requests:** {_failures(info.get('failures'), info.get('max_failure_ratio'))}")
    if isinstance(info.get("failures"), dict) and info["failures"].get("exceptions"):
        lines.append(f"- **Load generator exceptions:** {info['failures']['exceptions']}")
    lines.append(f"- **Window:** {_window(info.get('started'), info.get('ended'))}")
    if info.get("run_info_error"):
        lines.append(f"- **Metadata missing:** {info['run_info_error']}")
    return "\n".join(lines)


def _case(info: dict) -> str:
    case = info.get("case")
    lines = [f"## Test case `{info.get('test_case') or UNKNOWN}`", ""]
    if isinstance(case, dict) and case:
        lines += _table(["Key", "Value"], [[k, v] for k, v in case.items()])
    else:
        lines.append("The resolved case was not recorded.")

    users, rate = info.get("users"), info.get("spawn_rate")
    if isinstance(users, (int, float)) and isinstance(rate, (int, float)) and rate > 0:
        lines.append("")
        if rate >= users:
            lines.append(f"All {users} users start at once.")
        else:
            lines.append(f"The {users} users start over the first {users / rate:g} s, at {rate:g}/s.")

    flags = info.get("worker_flags")
    if isinstance(flags, dict) and flags:
        lines.append("")
        lines.append("Worker flags as run: " + " ".join(f"`--{k}={v}`" for k, v in flags.items()))
    return "\n".join(lines)


def _hardware(info: dict) -> str:
    cluster = info.get("cluster") if isinstance(info.get("cluster"), dict) else {}
    pools = cluster.get("node_pools") if isinstance(cluster.get("node_pools"), dict) else {}
    nodes = info.get("nodes") if isinstance(info.get("nodes"), dict) else {}

    lines = ["## Hardware", ""]
    if not (cluster or nodes):
        lines.append("Neither the cluster's configuration nor its nodes were recorded.")
        return "\n".join(lines)
    if cluster:
        lines.append(
            f"GKE cluster `{cluster.get('name') or UNKNOWN}` in {cluster.get('location') or UNKNOWN}: "
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


def _workers(info: dict) -> str:
    pool = info.get("worker_pool") if isinstance(info.get("worker_pool"), dict) else {}
    templates = info.get("actor_templates") if isinstance(info.get("actor_templates"), dict) else {}
    configs = info.get("sandbox_configs") if isinstance(info.get("sandbox_configs"), dict) else {}
    if not (pool or templates):
        return ""

    lines = ["## Workers and actors", ""]
    if pool:
        spec, status = pool.get("spec") or {}, pool.get("status") or {}
        lines.append(
            f"WorkerPool: {status.get('readyReplicas', 0)}/{spec.get('replicas', UNKNOWN)} workers ready, "
            f"sandbox class {spec.get('sandboxClass') or UNKNOWN}."
        )
    for name, tmpl in templates.items():
        if not isinstance(tmpl, dict):
            continue
        sandbox = tmpl.get("sandboxConfig") or {}
        config_name = sandbox.get("configName")
        parts = [f"sandbox config `{config_name or UNKNOWN}`"]
        runtime = _sandbox_asset(configs.get(config_name))
        if runtime:
            parts.append(f"runtime `{runtime}`")
        memory = [
            r.get("quantity") for r in (tmpl.get("resources") or {}).get("limits") or [] if r.get("name") == "memory"
        ]
        parts.append(f"memory limit {memory[0] if memory else 'unset'}")
        snapshots = tmpl.get("snapshotsConfig") or {}
        if snapshots.get("onPause"):
            parts.append(f"snapshot on pause {snapshots['onPause'].removeprefix('SNAPSHOT_CONTENT_SCOPE_')}")
        golden = ((tmpl.get("status") or {}).get("goldenSnapshotStatus") or {}).get("takeGoldenSnapshotAt")
        if golden:
            parts.append(f"golden snapshot taken {golden}")
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
    if info.get("locust_image"):
        rows.append(["Load generator image", f"`{info['locust_image']}`"])

    for name, tmpl in (info.get("actor_templates") or {}).items():
        if isinstance(tmpl, dict):
            for c in tmpl.get("containers") or []:
                rows.append([f"ActorTemplate `{name}` / {c.get('name')}", f"`{c.get('image')}`"])
    for name, dep in (info.get("deployments") or {}).items():
        if isinstance(dep, dict):
            for c in dep.get("containers") or []:
                rows.append([f"Deployment `{name}` / {c.get('name')} ×{dep.get('replicas', UNKNOWN)}", f"`{c.get('image')}`"])

    if not rows:
        return ""
    return "\n".join(["## Versions and images", "", *_table(["Component", "Commit or image"], rows)])


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
    if not isinstance(failures, dict):
        return UNKNOWN
    if failures.get("requests") == 0:
        return "none; the run made no requests"
    text = f"{failures['failed']:,} of {failures['requests']:,} ({failures['ratio']:.3%})"
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


def _sandbox_asset(config) -> str:
    """The amd64 runtime the SandboxConfig installs, which is what the nodes run."""
    if not isinstance(config, dict):
        return ""
    for asset in ((config.get("assets") or {}).get("amd64") or {}).values():
        if isinstance(asset, dict) and asset.get("url"):
            return asset["url"]
    return ""


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
    return f"{scaling.get('min')}–{scaling.get('max')} per zone"


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
