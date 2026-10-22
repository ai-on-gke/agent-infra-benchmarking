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

"""The sandbox cost sheet: its suspend and restore overheads from the runs a
sandbox-benchmark comparison combines, the rest from the prices and usage
below. sandbox_benchmark.summarize writes it as CSV beside perf_results.csv for
a case with `cost-sheet: true`. An overhead is the provider's mean Suspend or
Resume of test_suspend_resume, as perf_results.csv has it, taken once an
hour."""

from __future__ import annotations

import statistics

from .log import log

COST_SHEET_CSV = "cost_sheet.csv"

TEST = "test_suspend_resume"
ACTIVE_SECS_PER_HOUR = 360
HOURS_PER_MONTH = 270
VCPUS = 2
RAM_GIB = 2

# Per vCPU-second and per GiB-second, in USD. substrate's is its share of the
# case's n2 node, at us-central1's on-demand prices.
RESOURCE_PRICED = {
    "substrate": ("Substrate (GKE n2)", 0.031611 / 3600, 0.004237 / 3600),
    "e2b": ("E2B", 0.000014, 0.0000045),
    "modal": ("Modal", 0.00001971, 0.00000672),
    "daytona": ("Daytona", 0.000014, 0.0000045),
}
# Per session-hour, in USD, billed while the session runs; model tokens are not included.
ANTHROPIC_SESSION_HOUR = 0.08


def overhead(runs: list[dict], provider: str, metric: str) -> float | None:
    """The mean, in seconds, of the provider's runs' mean metric."""
    means = [
        statistics.fmean(r["metrics"][metric])
        for r in runs
        if r["provider"] == provider and r["test"] == TEST and r["metrics"].get(metric)
    ]
    return statistics.fmean(means) / 1000 if means else None


def sample_count(runs: list[dict]) -> int:
    counts = {}
    for r in runs:
        if r["test"] == TEST:
            counts[r["provider"]] = counts.get(r["provider"], 0) + 1
    return max(counts.values(), default=0)


def sheet(sources: list[dict]) -> list[list[str]]:
    """The sheet's rows, of the combined sources, each column of the source of
    its provider."""
    runs = [{**r, "provider": s["provider"]} for s in sources for r in s["runs"]]
    providers = {s["provider"] for s in sources}
    for p in [*RESOURCE_PRICED, "anthropic", "openai"]:
        if p not in providers:
            log(f"sandbox-benchmark: no {p} runs in the results; its cost sheet column is left empty", "WARNING")
        failed = sum(1 for r in runs if r["provider"] == p and r["test"] == TEST and r["errors"])
        if failed:
            log(f"sandbox-benchmark: {failed} of {p}'s runs failed; the cost sheet still counts them", "WARNING")

    cols = {p: {} for p in RESOURCE_PRICED}
    for p, (_, vcpu_price, ram_price) in RESOURCE_PRICED.items():
        suspend, resume = overhead(runs, p, "Suspend"), overhead(runs, p, "Resume")
        if suspend is None or resume is None:
            continue
        billable = ACTIVE_SECS_PER_HOUR + suspend + resume
        vcpu_hour = billable * vcpu_price * VCPUS
        ram_hour = billable * ram_price * RAM_GIB
        cols[p] = {
            "suspend": f"{suspend:.5f}",
            "resume": f"{resume:.5f}",
            "billable": f"{billable:.5f}",
            "vcpu_price": f"${vcpu_price * VCPUS:.8f}",
            "ram_price": f"${ram_price:.8f}",
            "vcpu_hour": f"${vcpu_hour:.7f}",
            "ram_hour": f"${ram_hour:.7f}",
            "month": f"${(vcpu_hour + ram_hour) * HOURS_PER_MONTH:.2f}",
        }

    def anthropic(key: str) -> str:
        if "anthropic" not in providers:
            return ""
        session_hour = ACTIVE_SECS_PER_HOUR * ANTHROPIC_SESSION_HOUR / 3600
        return {
            "suspend": "N/A",
            "resume": "N/A",
            "vcpu_price": "N/A",
            "ram_price": "N/A",
            "session_price": f"${ANTHROPIC_SESSION_HOUR / 3600:.8f}",
            "vcpu_hour": "N/A",
            "ram_hour": "N/A",
            "session_hour": f"${session_hour:.4f}",
            "month": f"${session_hour * HOURS_PER_MONTH:.2f}",
        }.get(key, "")

    def openai(key: str) -> str:
        if "openai" not in providers:
            return ""
        return {"suspend": "N/A", "resume": "N/A", "month": "No Public Pricing Available"}.get(key, "")

    def row(label: str, key: str, resource_default: str = "") -> list[str]:
        return (
            [label]
            + [cols[p].get(key, resource_default if cols[p] else "") for p in RESOURCE_PRICED]
            + ["", anthropic(key), openai(key)]
        )

    width = len(RESOURCE_PRICED) + 4
    blank = [""] * width
    pad = blank[1:]
    return [
        blank,
        [
            "", "Resource-based Pricing Models", *[""] * len(RESOURCE_PRICED),
            "Agents-as-a-Service - Session-based Pricing", "",
        ],  # fmt: skip
        ["", *(name for name, _, _ in RESOURCE_PRICED.values()), "", "Anthropic Managed Agents", "OpenAI Frontier"],
        [
            "Active Time (seconds per hour)", *[str(ACTIVE_SECS_PER_HOUR)] * len(RESOURCE_PRICED), "",
            *[str(ACTIVE_SECS_PER_HOUR)] * 2,
        ],  # fmt: skip
        row("Suspend Overhead (seconds per hour)", "suspend"),
        row("Restore Overhead (seconds per hour)", "resume"),
        row("Billable Time (s)\n(assumes all overhead time is billed)", "billable"),
        blank,
        ["Pricing"] + pad,
        row(f"vCPU ({VCPUS} vCPU/s)", "vcpu_price"),
        row("RAM (GiB/s)", "ram_price"),
        row("Session Cost (per second)", "session_price", "N/A"),
        blank,
        ["Cost"] + pad,
        row("vCPU Cost (per hour)", "vcpu_hour"),
        row("RAM Cost (per hour)", "ram_hour"),
        row("Session Cost (per hour)", "session_hour", "N/A"),
        row("Total Cost (per month)", "month"),
        blank,
        blank,
        ["Caveats"] + pad,
        [f"1) Results assume linear scaling of the samples (n={sample_count(runs)})"] + pad,
        ["2) A simple, representative workload is used to provide a similar filesystem state for all tests"] + pad,
        [
            "3) All tests performed on non-premium tiers of product offerings.  Optimizations for high scale,"
            " paying customers are likely and not represented in these results"
        ]
        + pad,
        ["4) Storage costs for snapshots or filesystem persistance are not included"] + pad,
        ["5) Anthropic Managed Agents' model token costs are not included"] + pad,
        [
            "6) Substrate is priced as its sandbox's share of an on-demand GKE n2 node; the node's idle capacity"
            " and GKE's cluster fee are not included"
        ]
        + pad,
    ]
