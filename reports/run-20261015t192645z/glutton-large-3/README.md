# glutton-large, repeat 3 of 3, run run-20261015t192645z

- **Result:** passed (exit 0)
- **Failed cycles:** 0 of 29,189 (0.000%), allowed 1%
- **Failed cycles, steady state:** 0 of 27,470 (0.000%)
- **Window:** 2026-10-15 21:25:43 to 21:31:00 UTC (5m 17s)
- **Steady-state window:** 2026-10-15 21:26:11 to 21:30:50 UTC (4m 39s), from 10s after the last user started

## Results

From `locust_stats_steady.csv`; latencies in ms.

| Request | Count | Failed | p50 | p90 | p99 | Max |
|---|---|---|---|---|---|---|
| `Resume` | 27477 | 0 | 230 | 290 | 1000 | 3284 |
| `ExecutionTime` | 27477 | 0 | 4 | 5 | 6 | 16 |
| `TTFI` | 27477 | 0 | 240 | 290 | 1000 | 3289 |
| `Suspend` | 27470 | 0 | 440 | 530 | 2400 | 14611 |
| `CycleTime` | 27470 | 0 | 680 | 800 | 3400 | 14855 |

## Test case `glutton-large`

| Key | Value |
|---|---|
| worker-count | 100 |
| sandbox-class | gvisor |
| max-failure-ratio | 0.01 |
| locust-workers | 4 |
| type | locust |
| repeats | 3 |
| actor-memory | 256Mi |

The 100 users start over the first 10 s, at 10/s.

Master flags as run: `--run-time=5m` `--users=100` `--spawn-rate=10` `--warm-up=10s`

Worker flags as run: `--actor-egress-allow=opentelemetry-collector.gke-managed-otel.svc.cluster.local` `--workload-name=glutton` `--workload-template=glutton` `--resume-mode=explicit`

## Hardware

GKE cluster `run-20261015t192645z-2fd27040` in us-central1-a: control plane 1.35.8-gke.1626001, release channel REGULAR.

| Node pool | Nodes ready | Machine | vCPU | Memory | Boot disk | Local SSD | Node version | Auto-upgrade | Autoscaling | Zones |
|---|---|---|---|---|---|---|---|---|---|---|
| default | 1/1 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |
| monitoring | 1/1 | e2-standard-4 | 4 | 15.6 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |
| sandboxed | 25/25 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 25 per zone | us-central1-a |

## Workers and actors

WorkerPool: 100/100 workers ready, sandbox class gvisor.

ActorTemplate `glutton`: sandbox config `gvisor-default`, runtime `gvisor` (sha256 `d547d81401461fd1c679c5c4fa0a6c2b8ef7dc3c22ce23c9e25dcc4c69cfd06f`, from `gs://gvisor/releases/nightly/2026-09-02/x86_64/gvisor.tar.zstd`), memory limit 256Mi, snapshot scope FULL, golden snapshot taken 2026-10-15T21:25:14.172400338Z.

## Versions and images

| Component | Commit or image |
|---|---|
| Repository commit | `1aa312ccb61e0636c73a98022cac97ae7e5c1628` (uncommitted changes) |
| Substrate commit | `756c2a53741121e728f4cc3066c8a19e575b4919` |
| Substrate build version | `v0.4.0` |
| Image `locust-master` | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/locust-master@sha256:8ac3fd6c420ac9aecf5f1d435b21dfd5f7272525244e630e95363d24fc8934a9` |
| Image `boomer-worker` | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/boomer-worker@sha256:e53a67a64f60b44a0b6af63705eb1ab006f6565873f4c81f1e973a8ac9e93898` |
| ActorTemplate `glutton` / glutton | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/glutton@sha256:3df5cafe2a9b9c492f5130dbc9414180508612f8e8abf918c7f1ff828eacb7a8` |
| Deployment `benchmark-ateom` / ateom x100 | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/ateom-gvisor@sha256:56e8c88e6ae022a7e1144e3a36212bd391f51ca1c1a862c968eb3856cffc1130` |
| Control plane `DaemonSet/atelet-v0-4-0` / atelet | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/atelet@sha256:126725f720b8d40cf8e93cd08c9935af40c58c8df02f139e9cbc6c0f92a1bcc0` |
| Control plane `Deployment/ate-api-server` / ate-api-server | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/ateapi@sha256:d1902e37be73e25d6c7a82c6c9b9555c444dc8dd388ff604d0696a28899f0b64` |
| Control plane `Deployment/ate-controller` / ate-controller | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/atecontroller@sha256:c498808de2651d14c58ccf4daba4d8bac91befb415861e58bddc5063726df643` |
| Control plane `Deployment/atenet-egress` / sdsmint | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/atenet@sha256:394000ea68a3c916c0d4774efa9d5bc0bb2d461bbb73e6a69654a77141b48675` |
| Control plane `Deployment/atenet-egress` / envoy | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/envoy-dataplane@sha256:dc30172033e2db5f77094133be52b7de69ceed42c298e4dd3cf842f4f9bccc48` |
| Control plane `Deployment/atenet-egress` / ext-proc | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/atenet@sha256:394000ea68a3c916c0d4774efa9d5bc0bb2d461bbb73e6a69654a77141b48675` |
| Control plane `Deployment/atenet-router` / atenet-router | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t192645z-064f586e/atenet@sha256:394000ea68a3c916c0d4774efa9d5bc0bb2d461bbb73e6a69654a77141b48675` |
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
