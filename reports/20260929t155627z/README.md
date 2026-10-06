# Example run `20260929t155627z`

A single committed run, kept as the worked example for
[Reading the results](../../README.md#reading-the-results) in the top-level
README. The metric definitions, the authoritative-file rule and the ramp/stall
caveats all live there; this file is just what *this* run was and what it
showed.

## What it is

- **Case:** `glutton` (see [`benchmarking/tests.yaml`](../../benchmarking/tests.yaml)).
- **Load:** 100 users, `spawn-rate: 2` (so the first ~50 s are a ramp), 5 min.
- **Workers:** 100 gVisor workers (`worker-count: 100`, `sandbox-class: gvisor`).
- **Hardware:** this run predates the `run.json` metadata that now records the
  `terraform.tfvars`, so the hardware is stated from the review that reproduced
  it: ~30 `c2-standard-16` gVisor worker nodes in `us-central1-a` (the shipped
  `sandboxed` pool) plus the `default` node. Future runs capture this in
  `run.json` instead of a sentence here.
- **Sample size:** one. Not a repeated measurement — see
  [Repeats](../../README.md#repeats) before comparing it to anything.

## Headline numbers

From `locust_stats.csv` (the authoritative file; `stats.csv` is the coarse
Prometheus mirror). Milliseconds.

| Row | count | fail | p50 | p90 | p99 | p99.9 | max | req/s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `TTFI` | 24894 | 1 | 250 | 380 | 1000 | 3300 | ~13000 | 83.3 |
| `CycleTime` | 24827 | 2 | 760 | 1100 | 2400 | 4900 | 13958 | 83.0 |
| `Resume` | 24894 | 0 | 250 | 380 | 1000 | 3300 | 13378 | 83.3 |
| `Suspend` | 24826 | 1 | 480 | 690 | 1700 | 3200 | 12238 | 83.0 |
| `ExecutionTime` | 24894 | 1 | 4 | 5 | 7 | 8 | 24 | 83.3 |

Throughput saturates at ~83 resume/s. Five requests failed over the run
(`locust_failures.csv`): four are one `HTTP 421: misdirected request` cycle
(the router used a worker assignment that had just gone stale), and one is a
`Suspend` that came back `Unavailable`.

## The p99 is a handful of stalls, not a slow run

`TTFI` p50 sits at ~230–250 ms for the entire run. The tail is carried by a
few multi-second stalls, not by a uniformly slower cluster:

- t≈40–45 s — max ~9.1 s (still inside the ramp)
- t≈185–188 s — max ~10 s, and p99 for those seconds climbs to ~1.3–1.6 s
- t≈225–232 s — max ~13 s

Each stall spans several consecutive one-second samples (locust's sliding
window), which is why the aggregate p99 lands near 1 s even though almost every
individual resume is ~250 ms. Read p50 as the headline; the ~1 s p99 is these
stalls and should be attributed (the leading suspect is node-level I/O
pressure) before it is quoted on its own.
