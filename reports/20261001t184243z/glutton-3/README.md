# glutton, repeat 3 of 3 — run 20261001t184243z

- **Result:** passed (exit 0)
- **Failed requests:** 0 of 85,926 (0.000%), allowed 1%
- **Window:** 2026-10-01 18:53:44 to 18:57:07 UTC (3m 23s)
- **Steady-state window:** 2026-10-01 18:54:26 to 18:56:44 UTC (2m 17s), from 30s after the last user started

## Test case `glutton`

| Key | Value |
|---|---|
| duration | 3m |
| max-failure-ratio | 0.01 |
| repeats | 3 |
| resume-mode | explicit |
| sandbox-class | gvisor |
| spawn-rate | 10 |
| users | 100 |
| worker-count | 100 |
| workload-name | glutton |
| workload-template | glutton |

The 100 users start over the first 10 s, at 10/s.

Worker flags as run: `--workload-name=glutton` `--workload-template=glutton` `--resume-mode=explicit` `--actor-egress-allow=opentelemetry-collector.gke-managed-otel.svc.cluster.local`

## Hardware

GKE cluster `akamalov-test` in us-central1: control plane 1.35.8-gke.1225000, release channel STABLE.

| Node pool | Nodes ready | Machine | vCPU | Memory | Boot disk | Local SSD | Node version | Auto-upgrade | Autoscaling | Zones |
|---|---|---|---|---|---|---|---|---|---|---|
| akamalov-test-default | 1/1 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1225000 | off | fixed at 1 per zone | us-central1-a |
| akamalov-test-sandboxed | 30/30 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1225000 | off | fixed at 30 per zone | us-central1-a |

## Workers and actors

WorkerPool: 100/100 workers ready, sandbox class gvisor.

ActorTemplate `glutton`: sandbox config `gvisor-default`, runtime `gs://gvisor/releases/nightly/2026-09-02/x86_64/gvisor.tar.zstd`, memory limit 256Mi, snapshot on pause FULL, golden snapshot taken 2026-10-01T18:42:02.607296603Z.

## Versions and images

| Component | Commit or image |
|---|---|
| Repository commit | `7d12f61ac9df754f06c7de241764336367badcd4` |
| Substrate commit | `6ec93a45f0fe4b935d550a505321c08185458c39` |
| Load generator image | `us-docker.pkg.dev/akvelon-gke-aieco/akamalov-test/locust-test@sha256:6aa4b675497a82821d8abb05ad623fae55577d5e30ed76c84e3a27569ed22cc2` |
| ActorTemplate `glutton` / glutton | `us-docker.pkg.dev/akvelon-gke-aieco/akamalov-test/glutton-cfe962bb0fa41a57d9c1bc35ec50d6e9@sha256:4a2ef866d538a041c87d701941e6789a9580a033013cf2f275a700fbe077c3aa` |
| Deployment `benchmark-ateom` / ateom ×100 | `us-docker.pkg.dev/akvelon-gke-aieco/akamalov-test/ateom-gvisor-715889664656de67e44382a8d6ab981d@sha256:0112089bc92ddfa95390beaeb995fd0c029642f7700bec22abb77040f4d88931` |
| Deployment `mock-llm` / mock-llm ×10 | `us-docker.pkg.dev/akvelon-gke-aieco/akamalov-test/mock-llm@sha256:bcc98062c07861aa46ca9d8eacd39e0cbc87c45708742282df404cc87b908239` |

## Files

- `locust_stats_steady.csv`: the run's latency percentiles: locust's own totals per request name over the steady-state window only. Failed requests included, rounded to 1 ms below 100 ms, 10 ms below 1 s, 100 ms below 10 s and 1 s above.
- `locust_stats.csv`: the same over the whole run, including the ramp, the warm-up and the teardown.
- `locust_stats_history.csv`: the same, one row per second per request name, with the user count at that second.
- `locust_failures.csv`: failed requests, grouped by error.
- `locust_exceptions.csv`: errors raised inside the load generator.
- `custom_stats_history.csv`: active and idle actors over the run, from Prometheus.
- `cluster_vitals_history.csv`: CPU and memory of every node over the run; worker nodes have Pool=worker.
- `run.json`: everything above in full: the resolved case, the cluster and its nodes, the WorkerPool, ActorTemplate and SandboxConfig, every image by digest, and the commits.
