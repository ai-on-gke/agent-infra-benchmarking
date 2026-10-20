# glutton-microvm, repeat 2 of 3, run run-20261015t144424z

- **Result:** passed (exit 0)
- **Failed cycles:** 0 of 1,134 (0.000%), allowed 1%
- **Failed cycles, steady state:** 0 of 1,097 (0.000%)
- **Window:** 2026-10-15 17:34:25 to 17:39:49 UTC (5m 23s)
- **Steady-state window:** 2026-10-15 17:34:44 to 17:39:32 UTC (4m 47s), from 10s after the last user started

## Results

From `locust_stats_steady.csv`; latencies in ms.

| Request | Count | Failed | p50 | p90 | p99 | Max |
|---|---|---|---|---|---|---|
| `Resume` | 1095 | 0 | 920 | 1100 | 1400 | 2485 |
| `ExecutionTime` | 1095 | 0 | 6 | 15 | 39 | 60 |
| `TTFI` | 1095 | 0 | 930 | 1100 | 1400 | 2491 |
| `Suspend` | 1097 | 0 | 1400 | 1900 | 2600 | 4999 |
| `CycleTime` | 1097 | 0 | 2300 | 2900 | 3600 | 5835 |

## Test case `glutton-microvm`

| Key | Value |
|---|---|
| worker-count | 10 |
| sandbox-class | microvm |
| max-failure-ratio | 0.01 |
| locust-workers | 1 |
| type | locust |
| repeats | 3 |
| actor-memory | 256Mi |

All 10 users start at once.

Master flags as run: `--run-time=5m` `--users=10` `--spawn-rate=10` `--warm-up=10s`

Worker flags as run: `--actor-egress-allow=opentelemetry-collector.gke-managed-otel.svc.cluster.local` `--workload-name=glutton` `--workload-template=glutton` `--resume-mode=explicit`

## Hardware

GKE cluster `run-20261015t144424z-1514b318` in us-central1-a: control plane 1.35.8-gke.1626001, release channel REGULAR.

| Node pool | Nodes ready | Machine | vCPU | Memory | Boot disk | Local SSD | Node version | Auto-upgrade | Autoscaling | Zones |
|---|---|---|---|---|---|---|---|---|---|---|
| default | 1/1 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |
| monitoring | 1/1 | e2-standard-4 | 4 | 15.6 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |
| sandboxed | 1/1 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |

## Workers and actors

WorkerPool: 10/10 workers ready, sandbox class microvm.

ActorTemplate `glutton`: sandbox config `microvm`, runtime `cloud-hypervisor` (sha256 `448af3d4e59b22c2987f7df94c213ad40fb53a10d437e42b5ee6c4fce7c29ecc`), `kata-image` (sha256 `96497f64da1de9c7473fef46c3d29ddd0805d334731cf9d903a21a5b2c33cefb`), `kata-kernel` (sha256 `8e9dbc3a6e4c26adb089d23d31201edebce905c7f3e31aa67b32bdd41df1b86f`), `virtiofsd` (sha256 `15b2e72a78cc08a9bd8a6943e89fb69c88cb3cbeb63069efceade835342ac7d4`), memory limit 256Mi, snapshot scope FULL, golden snapshot taken 2026-10-15T17:33:47.778000449Z.

## Versions and images

| Component | Commit or image |
|---|---|
| Repository commit | `1aa312ccb61e0636c73a98022cac97ae7e5c1628` (uncommitted changes) |
| Substrate commit | `756c2a53741121e728f4cc3066c8a19e575b4919` |
| Substrate build version | `v0.4.0` |
| Image `locust-master` | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/locust-master@sha256:0f80f4d619ca693c287d21c60783c0bfa0bb1b5a5ad9e3f86e2be0b6096f6783` |
| Image `boomer-worker` | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/boomer-worker@sha256:6550bc271de98da4df97d8a1ba97533cd0db6dccef5a33134ec0a4c7fac7057d` |
| ActorTemplate `glutton` / glutton | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/glutton@sha256:3df5cafe2a9b9c492f5130dbc9414180508612f8e8abf918c7f1ff828eacb7a8` |
| Deployment `benchmark-ateom` / ateom x10 | `us-central1-docker.pkg.dev/akvelon-gke-aieco/run-20261015t144424z-23d2e428/ateom-microvm@sha256:04b597f408261e93d0f1bf5efbafdfd1e4ec918d955b47c6fb25eeb30eaab4bf` |
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
