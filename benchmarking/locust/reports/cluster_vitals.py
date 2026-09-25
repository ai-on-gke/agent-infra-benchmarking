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

"""The cluster's CPU and memory over the run, from cAdvisor, in the same shape
as custom_stats_history.csv. Type is the node, or "cluster" for all of them.

Each value is a node's root cgroup (id="/"), so it covers everything on the
node and not only the run's pods. The series carry no run_id: the run is
picked out by its time window alone.

Utilization is against the node's capacity, not its allocatable.
"""

from .base import RunContext
from .custom_history import STEP_SECS, USERS_QUERY, history_csv
from .prometheus import range_query, series, series_by

# Every node is scraped through the one API server address, so `instance` is
# the same on all of them; the hostname label the scrape copies off the node
# is what tells them apart.
NODE_LABEL = "kubernetes_io_hostname"

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


class ClusterVitalsReport:
    name = "cluster-vitals"
    filename = "cluster_vitals_history.csv"

    def render(self, ctx: RunContext) -> str:
        samples: dict[tuple[str, str], dict[int, float]] = {}
        for name, query in METRICS:
            nodes = series_by(self._query(ctx, query, NODE_LABEL), NODE_LABEL)
            for node in sorted(nodes):
                samples[(node, name)] = nodes[node]
            samples[("cluster", name)] = series(self._query(ctx, query, ""))
        if not any(samples.values()):
            raise ValueError("no cAdvisor node series in the run's window")

        users = series(self._range(ctx, USERS_QUERY.format(run_id=ctx.run_id)))
        return history_csv(users, samples)

    def _query(self, ctx: RunContext, query: str, by: str) -> list[dict]:
        return self._range(ctx, query.format(by=by, window=RATE_WINDOW))

    def _range(self, ctx: RunContext, query: str) -> list[dict]:
        return range_query(ctx.prometheus_url, query, ctx.started, ctx.ended, STEP_SECS)
