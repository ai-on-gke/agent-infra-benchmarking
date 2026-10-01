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

"""Minimal Prometheus range-query client.

Standard library only: the reports need two endpoints, and the official
client is a metrics exporter rather than a query API.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://prometheus.benchmarking.svc.cluster.local:9090"


class PrometheusError(Exception):
    pass


def range_query(
    base_url: str,
    query: str,
    start: float,
    end: float,
    step: int,
    timeout: float = 15.0,
) -> list[dict]:
    """Run one range query and return its raw result series."""
    params = urllib.parse.urlencode(
        {"query": query, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": step}
    )
    url = f"{base_url.rstrip('/')}/api/v1/query_range?{params}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise PrometheusError(f"{url}: {exc}") from exc
    if payload.get("status") != "success":
        raise PrometheusError(payload.get("error", "query rejected"))
    return payload["data"]["result"]


def instant_query(base_url: str, query: str, timeout: float = 15.0) -> list[dict]:
    """Run one instant query and return its raw result series."""
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise PrometheusError(f"{url}: {exc}") from exc
    if payload.get("status") != "success":
        raise PrometheusError(payload.get("error", "query rejected"))
    return payload["data"]["result"]


def series(result: list[dict], index: int = 0) -> dict[int, float]:
    """One series as {unix_second: value}."""
    if not result:
        return {}
    return _samples(result[index])


def series_by(result: list[dict], label: str) -> dict[str, dict[int, float]]:
    """Every series as {its `label` value: {unix_second: value}}."""
    return {item["metric"].get(label, ""): _samples(item) for item in result}


def _samples(item: dict) -> dict[int, float]:
    """NaN samples are dropped — they mean the window held no observations,
    not that the value was zero."""
    return {int(float(ts)): float(val) for ts, val in item["values"] if val != "NaN"}
