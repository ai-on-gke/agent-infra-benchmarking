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

"""The cluster's CPU and memory over the run, from cAdvisor, into
cluster_vitals_history.csv in custom_history's shape. Type is the node, or
"cluster" for all of them.

Each value is a node's root cgroup (id="/"), so it covers everything on the
node and not only the run's pods. The series carry no run_id: the run is
picked out by its time window alone. Utilization is against the node's
capacity, not its allocatable.

Pool is "worker" on the rows of a node labeled benchmarking-worker=true, which
is where the WorkerPool schedules and what benchmark-charts draws, and empty
otherwise.
"""

from pathlib import Path

from custom_history import range_query, series, series_by, user_counts, write_history

FILENAME = "cluster_vitals_history.csv"

# Every node is scraped through the one API server address, so `instance` is
# the same on all of them; the hostname label the scrape copies off the node
# is what tells them apart.
NODE_LABEL = "kubernetes_io_hostname"

# The node label benchmarking-worker=true, as the scrape copies it: Prometheus
# replaces the dash, which a label name cannot hold.
WORKER_LABEL = "benchmarking_worker"
WORKER_POOL = "worker"
WORKER_NODES_QUERY = f'group by ({NODE_LABEL}) (machine_cpu_cores{{{WORKER_LABEL}="true"}})'

RATE_WINDOW = "1m"

# (Name, query). {by} is NODE_LABEL for the per-node rows and empty for the
# cluster row, so a cluster ratio is of the totals rather than a sum of ratios.
METRICS = (
    (
        "CPUCores",
        'sum by ({by}) (rate(container_cpu_usage_seconds_total{{id="/"}}[{window}]))',
    ),
    (
        "CPUUtilizationPercent",
        '100 * sum by ({by}) (rate(container_cpu_usage_seconds_total{{id="/"}}[{window}]))'
        " / sum by ({by}) (machine_cpu_cores)",
    ),
    (
        "MemoryWorkingSetBytes",
        'sum by ({by}) (container_memory_working_set_bytes{{id="/"}})',
    ),
    (
        "MemoryUtilizationPercent",
        '100 * sum by ({by}) (container_memory_working_set_bytes{{id="/"}})'
        " / sum by ({by}) (machine_memory_bytes)",
    ),
)


def write(csv_prefix: Path, started: float, ended: float) -> Path:
    """Writes the file beside locust's CSVs and returns its path."""
    values: dict[tuple[str, str], dict[int, float]] = {}
    for name, query in METRICS:
        nodes = series_by(range_query(query.format(by=NODE_LABEL, window=RATE_WINDOW), started, ended), NODE_LABEL)
        for node in sorted(nodes):
            values[(node, name)] = nodes[node]
        values[("cluster", name)] = series(range_query(query.format(by="", window=RATE_WINDOW), started, ended))
    if not any(values.values()):
        raise ValueError("Prometheus has no cAdvisor node series in the run's window")
    workers = series_by(range_query(WORKER_NODES_QUERY, started, ended), NODE_LABEL)
    pools = {node: WORKER_POOL for node in workers}
    return write_history(csv_prefix.parent / FILENAME, user_counts(csv_prefix), values, pools)
