# glutton-large, 3 repeats, run run-20261015t192645z

- **Summarized:** 3 of 3 repeats, the ones that passed
- **Failed cycles, all repeats:** 2 of 83,741 (0.002%), allowed 1%

| Repeat | Result | Failed cycles | Window | Steady-state window |
|---|---|---|---|---|
| glutton-large-1 | passed (exit 0) | 2 of 24,359 (0.008%) | 2026-10-15 19:57:07 to 20:02:24 UTC (5m 17s) | 2026-10-15 19:57:37 to 20:02:16 UTC (4m 39s) |
| glutton-large-2 | passed (exit 0) | 0 of 30,193 (0.000%) | 2026-10-15 20:43:04 to 20:48:19 UTC (5m 14s) | 2026-10-15 20:43:32 to 20:48:11 UTC (4m 39s) |
| glutton-large-3 | passed (exit 0) | 0 of 29,189 (0.000%) | 2026-10-15 21:25:43 to 21:31:00 UTC (5m 17s) | 2026-10-15 21:26:11 to 21:30:50 UTC (4m 39s) |

## Results

The median over the repeats that passed, with the lowest and highest in brackets, of each one's `locust_stats_steady.csv`; latencies in ms. `glutton-large-summary.csv` has every stat.

| Request | Requests/s | Average | p50 | p90 | p99 | p99.9 | Max |
|---|---|---|---|---|---|---|---|
| CreateActor | 0.01, in 1 | 1.5, in 1 | 2, in 1 | 2, in 1 | 2, in 1 | 2, in 1 | 2, in 1 |
| CreateActorEgressPolicy | 0.01, in 1 | 1, in 1 | 1, in 1 | 1, in 1 | 1, in 1 | 1, in 1 | 1, in 1 |
| CycleTime | 98.46 (81.99-102.39) | 765.48 (724.9-970.22) | 690 (680-750) | 800 (790-1200) | 3400 (1800-4300) | 4600 (3200-9100) | 14887 (14855-30309) |
| DeleteActor | 0.01, in 1 | 92.5, in 1 | 100, in 1 | 100, in 1 | 100, in 1 | 100, in 1 | 103, in 1 |
| ExecutionTime | 98.48 (81.98-102.44) | 3.99 (3.87-4.22) | 4 | 5 | 6 | 8 (8-9) | 22 (16-41) |
| Resume | 98.48 (81.98-102.44) | 261.41 (251.37-331.27) | 240 (230-250) | 290 (290-430) | 1000 (580-1500) | 1800 (1100-4700) | 6475 (3284-27180) |
| Suspend | 98.46 (81.99-102.39) | 499.33 (468.31-634.1) | 440 (440-470) | 530 (510-780) | 2400 (1300-3200) | 3400 (2700-6000) | 14611 (14555-30001) |
| TTFI | 98.48 (81.98-102.44) | 265.74 (256.11-335.73) | 240 (240-260) | 290 (290-430) | 1000 (590-1500) | 1800 (1100-4700) | 6480 (3289-27185) |
| Aggregated | 492.37 (409.95-512.1) | 359.14 (340.94-455.06) | 260 (260-300) | 700 (700-820) | 1700 (950-3200) | 3800 (2200-5500) | 14887 (14855-30309) |

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

GKE clusters `run-20261015t192645z-3233914d`, `run-20261015t192645z-ffaa2e76`, `run-20261015t192645z-2fd27040`, one per repeat, in us-central1-a: control plane 1.35.8-gke.1626001, release channel REGULAR.

| Node pool | Nodes ready | Machine | vCPU | Memory | Boot disk | Local SSD | Node version | Auto-upgrade | Autoscaling | Zones |
|---|---|---|---|---|---|---|---|---|---|---|
| default | 1/1 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |
| monitoring | 1/1 | e2-standard-4 | 4 | 15.6 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 1 per zone | us-central1-a |
| sandboxed | 25/25 | c2-standard-16 | 16 | 62.8 GiB | pd-balanced, 100 GB | none | v1.35.8-gke.1626001 | off | fixed at 25 per zone | us-central1-a |

## Workers and actors

WorkerPool: 100/100 workers ready, sandbox class gvisor.

ActorTemplate `glutton`: sandbox config `gvisor-default`, runtime `gvisor` (sha256 `d547d81401461fd1c679c5c4fa0a6c2b8ef7dc3c22ce23c9e25dcc4c69cfd06f`, from `gs://gvisor/releases/nightly/2026-09-02/x86_64/gvisor.tar.zstd`), memory limit 256Mi, snapshot scope FULL.

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

- `glutton-large-summary.csv`: the min, median and max of every stat in the `locust_stats_steady.csv` of the repeats that passed, with how many of them it was found in.
- `glutton-large-1/`: the repeat's own files, and its README.
- `glutton-large-2/`: the repeat's own files, and its README.
- `glutton-large-3/`: the repeat's own files, and its README.
