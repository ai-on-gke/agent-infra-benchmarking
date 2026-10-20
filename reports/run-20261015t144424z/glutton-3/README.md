# glutton, repeat 3 of 3, run run-20261015t144424z

- **Result:** passed (exit 0)
- **Failed cycles:** 0 of 2,533 (0.000%), allowed 1%
- **Failed cycles, steady state:** 0 of 2,434 (0.000%)
- **Window:** 2026-10-15 16:23:10 to 16:28:23 UTC (5m 12s)
- **Steady-state window:** 2026-10-15 16:23:27 to 16:28:15 UTC (4m 48s), from 10s after the last user started

## Results

From `locust_stats_steady.csv`; latencies in ms.

| Request | Count | Failed | p50 | p90 | p99 | Max |
|---|---|---|---|---|---|---|
| `Resume` | 2434 | 0 | 300 | 410 | 1300 | 6333 |
| `ExecutionTime` | 2434 | 0 | 4 | 5 | 8 | 12 |
| `TTFI` | 2434 | 0 | 310 | 410 | 1300 | 6339 |
| `Suspend` | 2434 | 0 | 520 | 740 | 1600 | 15569 |
| `CycleTime` | 2434 | 0 | 840 | 1100 | 2500 | 15864 |

## Test case `glutton`

| Key | Value |
|---|---|
| worker-count | 10 |
| sandbox-class | gvisor |
| max-failure-ratio | 0.01 |
| locust-workers | 1 |
| type | locust |
| repeats | 3 |
| actor-memory | 256Mi |

All 10 users start at once.

Master flags as run: `--run-time=5m` `--users=10` `--spawn-rate=10` `--warm-up=10s`

Worker flags as run: `--actor-egress-allow=opentelemetry-collector.gke-managed-otel.svc.cluster.local` `--workload-name=glutton` `--workload-template=glutton` `--resume-mode=explicit`

## Hardware

GKE cluster `run-20261015t144424z-deb49195` in us-central1-a: control plane 1.35.8-gke.1626001, release channel REGULAR.

| Node pool | Nodes ready | Machine | vCPU | Memory | Boot disk | Local SSD | Node version | Auto-upgrade | Autoscaling | Zones |
|---|---|---|---|---|---|---|---|---|---|---|
| default | 1/1 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |
| monitoring | 1/1 | e2-standard-4 | 4 | 15.6 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |
| sandboxed | 1/1 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |

## Workers and actors

WorkerPool: 10/10 workers ready, sandbox class gvisor.

ActorTemplate `glutton`: sandbox config `gvisor-default`, runtime `gvisor` (sha256 `d547d81401461fd1c679c5c4fa0a6c2b8ef7dc3c22ce23c9e25dcc4c69cfd06f`, from `gs://gvisor/releases/nightly/2026-09-02/x86_64/gvisor.tar.zstd`), memory limit 256Mi, snapshot scope FULL, golden snapshot taken 2026-10-15T16:22:38.611476674Z.

## Versions and images

| Component | Commit or image |
|---|---|
| Repository commit | `1aa312ccb61e0636c73a98022cac97ae7e5c1628` (uncommitted changes) |
| Substrate commit | `756c2a53741121e728f4cc3066c8a19e575b4919` |
| Substrate build version | `v0.4.0` |
| Image `locust-master` | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/locust-master@sha256:0f80f4d619ca693c287d21c60783c0bfa0bb1b5a5ad9e3f86e2be0b6096f6783` |
| Image `boomer-worker` | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/boomer-worker@sha256:6550bc271de98da4df97d8a1ba97533cd0db6dccef5a33134ec0a4c7fac7057d` |
| ActorTemplate `glutton` / glutton | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/glutton@sha256:3df5cafe2a9b9c492f5130dbc9414180508612f8e8abf918c7f1ff828eacb7a8` |
| Deployment `benchmark-ateom` / ateom x10 | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/ateom-gvisor@sha256:56e8c88e6ae022a7e1144e3a36212bd391f51ca1c1a862c968eb3856cffc1130` |
| Control plane `DaemonSet/atelet-v0-4-0` / atelet | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/atelet@sha256:126725f720b8d40cf8e93cd08c9935af40c58c8df02f139e9cbc6c0f92a1bcc0` |
| Control plane `Deployment/ate-api-server` / ate-api-server | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/ateapi@sha256:d1902e37be73e25d6c7a82c6c9b9555c444dc8dd388ff604d0696a28899f0b64` |
| Control plane `Deployment/ate-controller` / ate-controller | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/atecontroller@sha256:c498808de2651d14c58ccf4daba4d8bac91befb415861e58bddc5063726df643` |
| Control plane `Deployment/atenet-egress` / sdsmint | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/atenet@sha256:394000ea68a3c916c0d4774efa9d5bc0bb2d461bbb73e6a69654a77141b48675` |
| Control plane `Deployment/atenet-egress` / envoy | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/envoy-dataplane@sha256:53af9e0af970b9d58c6c54c718cc5cd935706fb61e7d4fc0a0e0fe9a76f12d85` |
| Control plane `Deployment/atenet-egress` / ext-proc | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/atenet@sha256:394000ea68a3c916c0d4774efa9d5bc0bb2d461bbb73e6a69654a77141b48675` |
| Control plane `Deployment/atenet-router` / atenet-router | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/atenet@sha256:394000ea68a3c916c0d4774efa9d5bc0bb2d461bbb73e6a69654a77141b48675` |
| Control plane `Deployment/atenet-router` / envoy | `envoyproxy/envoy@sha256:57e14a549d7bd43c8d3f6d03e8cfa653e037d4b38e133acd9b54f38c524401b4` |
| Control plane `StatefulSet/postgres` / tls-reloader | `postgres@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15` |
| Control plane `StatefulSet/postgres` / postgres | `postgres@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15` |
| Tools | terraform 1.16.4, kubectl v1.37.1, gcloud 573.0.0, go go1.27.1, docker 29.8.1, ko v0.19.1, python 3.14.8 |
| Images built with | cloud build, `gcr.io/cloud-builders/docker:24.0.9@sha256:9fcfbddaf676527b2be4a6473bb37ed028a88bc55bc5cd9dcc9db4f7d9bcebee`, `golang:1.27.2@sha256:007a316059246276cb214df93950be77837545dc78007a73e0291a4963d5d3bf` |

## Files

- `locust_stats_steady.csv`: the run's latency percentiles: locust's own totals per request name over the steady-state window only. Failed requests included, rounded to 1 ms below 100 ms, 10 ms below 1 s, 100 ms below 10 s and 1 s above.
- `locust_stats.csv`: the same over the whole run, including the ramp, the warm-up and the teardown.
- `locust_stats_history.csv`: the same, one row per second per request name, with the user count at that second.
- `locust_failures.csv`: failed requests, grouped by error.
- `locust_exceptions.csv`: errors raised inside the load generator.
- `custom_stats_history.csv`: active and idle actors over the run, from Prometheus.
- `cluster_vitals_history.csv`: CPU and memory of every node over the run; worker nodes have Pool=worker.
- `locust_steady_window.json`: the steady-state window's bounds, as the master measured them.
- `run.json`: everything above in full: the resolved case, the cluster and its nodes, the WorkerPool, ActorTemplate and SandboxConfig, every image by digest, and the commits.
- `cleanup.json`: written by the scenario-runner after the run: the actors it found left and deleted, and any still there a minute later.
