# glutton, 3 repeats — run 20261001t184243z

- **Summarized:** 3 of 3 repeats, the ones that passed
- **Failed requests, all repeats:** 18 of 248,724 (0.007%), allowed 1%

| Repeat | Result | Failed requests | Window | Steady-state window |
|---|---|---|---|---|
| glutton-1 | passed (exit 0) | 4 of 82,217 (0.005%) | 2026-10-01 18:44:15 to 18:47:37 UTC (3m 22s) | 2026-10-01 18:44:57 to 18:47:15 UTC (2m 17s) |
| glutton-2 | passed (exit 0) | 14 of 80,581 (0.017%) | 2026-10-01 18:48:52 to 18:52:29 UTC (3m 36s) | 2026-10-01 18:49:35 to 18:51:53 UTC (2m 18s) |
| glutton-3 | passed (exit 0) | 0 of 85,926 (0.000%) | 2026-10-01 18:53:44 to 18:57:07 UTC (3m 23s) | 2026-10-01 18:54:26 to 18:56:44 UTC (2m 17s) |

## Results

The median over the repeats that passed, with the lowest and highest in brackets, of each one's `locust_stats_steady.csv`; latencies in ms. `glutton-summary.csv` has every stat.

| Request | Requests/s | Average | p50 | p90 | p99 | p99.9 | Max |
|---|---|---|---|---|---|---|---|
| CreateActor | 0.02 (0.01–0.02), in 2 | 0.92 (0.33–1.5), in 2 | 1 (0–2), in 2 | 1.5 (1–2), in 2 | 1.5 (1–2), in 2 | 1.5 (1–2), in 2 | 1.5 (1–2), in 2 |
| CreateActorEgressPolicy | 0.02 (0.01–0.02), in 2 | 0.25 (0–0.5), in 2 | 0.5 (0–1), in 2 | 0.5 (0–1), in 2 | 0.5 (0–1), in 2 | 0.5 (0–1), in 2 | 0.5 (0–1), in 2 |
| CycleTime | 93.51 (92.14–95.85) | 830.56 (787.62–834.29) | 710 (690–730) | 910 (880–960) | 3400 (2600–3900) | 4800 (4200–4900) | 8252 (6404–17064) |
| DeleteActor | 0.02 (0.01–0.02), in 2 | 115.25 (97.5–133), in 2 | 120 (100–140), in 2 | 125 (100–150), in 2 | 125 (100–150), in 2 | 125 (100–150), in 2 | 125.5 (104–147), in 2 |
| ExecutionTime | 93.3 (92.11–95.85) | 3.47 (3.47–3.57) | 3 (3–4) | 4 | 6 (5–6) | 7 (7–8) | 15 (10–17) |
| Resume | 93.31 (92.11–95.86) | 267.9 (256.62–270.78) | 230 | 300 (290–320) | 1100 (920–1300) | 1800 (1700–3200) | 5489 (3478–16596) |
| Suspend | 93.51 (92.14–95.85) | 554.46 (526.21–555.47) | 470 (460–480) | 620 (600–650) | 2500 (2000–2800) | 3600 (3300–3900) | 8022 (6062–12244) |
| TTFI | 93.3 (92.11–95.85) | 272.34 (261.18–275.25) | 230 (230–240) | 300 (300–320) | 1100 (920–1300) | 1800 (1700–3200) | 5493 (3483–16600) |
| Aggregated | 466.96 (460.67–479.25) | 386.9 (367.04–386.93) | 260 (250–260) | 740 (710–750) | 1800 (1800–2300) | 4100 (3500–4300) | 8252 (6404–17064) |

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

- `glutton-summary.csv`: the min, median and max of every stat in the `locust_stats_steady.csv` of the repeats that passed, with how many of them it was found in.
- `glutton-1/`: the repeat's own files, and its README.
- `glutton-2/`: the repeat's own files, and its README.
- `glutton-3/`: the repeat's own files, and its README.
