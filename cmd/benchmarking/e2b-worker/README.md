# e2b-worker

The E2B counterpart of `boomer-worker`. It drives the E2B sandbox lifecycle
under load and reports through the boomer/locust worker protocol, so it appears
to the Python locust master as a regular worker and its rows sit alongside the
substrate rows for comparison.

Each VU goroutine creates one sandbox and then cycles it for the rest of the
run; the lifecycle logic lives in
[`internal/benchmarking/e2b/e2bworkload`](../../../internal/benchmarking/e2b/e2bworkload).

```
startUser:  CreateSandbox  ->  (setup) Pause
cycle (xN): ResumeSandbox  ->  Exec  ->  PauseSandbox
teardown:   KillSandbox
```

## Operation mapping to substrate (latency parity)

| substrate (runworkload) | e2b-worker | row name |
| --- | --- | --- |
| `CreateActor` | create a sandbox (boots a microVM) | `CreateSandbox` |
| `ResumeActor` | resume from pause | `ResumeSandbox` |
| instruction (HTTP via router) | `Process.Start` via envd | `Exec` |
| `SuspendActor` | pause | `PauseSandbox` |
| `DeleteActor` | kill | `KillSandbox` |
| `CreateAtespace` | — (E2B has no namespace) | — |

`TTFI` (resume wait) and `CycleTime` (resume+exec+pause) keep the **same** row
names as substrate because they measure the same thing on both sides.

Because E2B `create` boots a *running* microVM (substrate's `CreateActor` leaves
the actor suspended), `startUser` pauses once after create to reach the same
paused baseline every cycle starts from. That setup pause is **unmetered**, so
the `CreateSandbox` row stays a clean create-latency figure comparable to
`CreateActor`.

## Run

`E2B_API_KEY` is read from the environment. The worker connects to a locust
master (boomer's `--master-host` / `--master-port`, default `127.0.0.1:5557`).

```sh
export E2B_API_KEY=<your key>
go run ./cmd/benchmarking/e2b-worker/ --master-host <locust-master-host>
```

Against E2B Cloud the defaults (`--domain e2b.app`, `--template base`) work. For
a self-hosted E2B, pass `--domain <your-domain>` and a `--template` you have
built.

### Worker flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--domain` | `e2b.app` | E2B base domain. API host is `https://api.<domain>`. |
| `--template` | `base` | E2B template ID every sandbox is created from. |
| `--sandbox-timeout` | `300` | Sandbox TTL in seconds, applied on create and resume. |
| `--command` | `echo hello-from-e2b` | Instruction run inside each sandbox per cycle. |
| `--op-timeout` | `2m` | Ceiling on any single control-plane or envd call. |
| `--min-wait` / `--max-wait` | `0` / `500ms` | Think-time bounds between iterations. |
| `--trace-probability` | `0` | Fraction of iterations to sample a trace for. |
| `--prometheus-addr` | `:8001` | Address for the Prometheus `/metrics` endpoint. |

Boomer's own flags (`--master-host`, `--master-port`, `--max-rps`, profiling)
are also accepted; see `--help`.

## Not yet wired (operational)

This binary is the load engine. Running it through the shared
`benchmarking/run_locust_job.sh` harness the way substrate does still needs:

- a Docker image for `e2b-worker` (the harness currently builds only
  `boomer-worker`);
- a `provider: e2b` switch in `run_locust_job.sh` / `tests.yaml` to select this
  binary and drop substrate-only keys (`sandbox-class`, `workload-template`);
- a Python locust user class `benchmarking/locust/tests/e2blifecycle.py` named
  `E2BLifecycleUser` (matching `e2bworkload.E2BUserClass`);
- the `E2B_API_KEY` secret injected into the worker pod.

The in-cluster run against a **self-hosted** E2B also depends on that
deployment existing (the Terraform workstream). Until then the worker can be run
locally against E2B Cloud with a locally-run locust master.
