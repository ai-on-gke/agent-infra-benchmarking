# agent-infra-benchmarking
Backend for benchmarks and corresponding recipes for agent infra offerings.

This benchmark measures how long a client waits before a sandbox accepts
work — and it can drive that measurement against two different sandbox
control planes, selected with `--backend` on every script below:

- **`substrate`** (default, for backward compatibility) — the original
  target, [agent-substrate/substrate](https://github.com/agent-substrate/substrate).
- **`agent-sandbox`** — [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
  (`sigs.k8s.io/agent-sandbox`) instead.

See [Backends](#backends) below for what each one measures, how they map
onto each other, and what does not carry over between them.

## Preparation

### 1. Create a cluster

TBD. The cluster has to exist before any of the steps below; this repo does not
create one.

You can create one using terraform in the folder infra. Example tfvars should cover everything *EXPERIMENTAL STILL*.
For `--backend agent-sandbox`, a local `kind` cluster also works — that
backend needs no golden-snapshot infrastructure, unlike substrate.

### 2. Configure the environment

Copy the example and fill in the values for your project:

```bash
cp env.sh.example .env.sh
```

Which variables are actually read depends on `--backend` — see the comments
in `env.sh.example`.

### 3. Install the sandbox backend into the cluster

<details open>
<summary><strong>substrate</strong> (default)</summary>

Upstream's installer expects to run from the substrate repo root, so invoke it
through the wrapper:

```bash
./scripts/substrate/upstream.sh hack/install-ate.sh --deploy-ate-system
```

The wrapper sources `.env.sh`, regenerates
`dependency-repos/substrate/.ate-dev-env.sh` from it, and runs the given
executable with `dependency-repos/substrate` as the working directory. It takes
any upstream executable the same way:

```bash
./scripts/substrate/upstream.sh hack/install-ate.sh --help
```

</details>

<details>
<summary><strong>agent-sandbox</strong></summary>

```bash
export VERSION=v1.0.3   # see https://github.com/kubernetes-sigs/agent-sandbox/releases
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox-with-extensions.yaml
kubectl -n agent-sandbox-system rollout status deployment --timeout=180s
```

This installs both the core `Sandbox` CRD/controller and the extensions
(`SandboxTemplate`, `SandboxWarmPool`, `SandboxClaim`) this benchmark needs.
See the [agent-sandbox README](https://github.com/kubernetes-sigs/agent-sandbox#installation)
for selective-install and Helm options.

</details>

## Deploying the benchmarking

### 1. Deploy the locust stack

```bash
# substrate (default; --backend substrate is the same as omitting it):
./benchmarking/deploy_locust.sh --deploy \
  --user-class runworkload \
  --workload-name glutton \
  --workload-template glutton

# agent-sandbox:
./benchmarking/deploy_locust.sh --deploy --backend agent-sandbox
```

#### Options

- **`--backend NAME`** (default: `substrate`) — `substrate` | `agent-sandbox`.
  Forwarded to every stage below.

- **`--user-class NAME`** (default: `runworkload`) — the locust user class to
  run. It has to name a `benchmarking/locust/tests/NAME.py`, and this repo ships
  only `runworkload.py`, so `runworkload` is the sole value today. Under
  `--backend substrate` the worker binary also registers upstream's `glutton`
  and `durdir` classes, but their locust files live in substrate rather than
  here.

- **`--workload-name NAME`** — the workload a `RunWorkloadUser` drives,
  meaning what counts as an instruction the sandbox demonstrably ran. Each
  workload targets exactly one backend:
  - `--backend substrate` (default `glutton`): `glutton` — POSTs a protobuf
    ping to `/ping` and verifies the echoed nonce; `openclaw` — POSTs a
    fast-path prompt to `/invoke` and checks the reply.
  - `--backend agent-sandbox` (default and only `ping`): POSTs a JSON nonce
    to `/ping` and verifies the echo — see [benchmarking/workloads/probe](benchmarking/workloads/probe).

- **`--workload-template NAME`** — the ActorTemplate (substrate) or
  SandboxTemplate (agent-sandbox) the workload runs on; defaults to the
  workload's own name (`glutton`, `openclaw`, or `ping`).

This runs three stages in order: `locust/build_and_push.sh` builds and pushes
the locust image (which carries the `boomer-worker` Go binary, usable by
either backend) and whichever workload image(s) the chosen `--backend` needs;
`workloads/deploy.sh --backend ...` applies that backend's workload
infrastructure and waits for it to be ready; `locust/deploy.sh --backend ...`
deploys locust into the `benchmarking` namespace. See
`./benchmarking/deploy_locust.sh --help` for the rest of the flag surface —
`--sandbox-class`/`--actor-memory`/`--otlp-endpoint` are substrate-only,
`--runtime-class`/`--probe-memory` are agent-sandbox-only, and
`--worker-count`/`--wait-timeout` apply to both.

What gets measured is chosen here rather than in the web UI, because these
values are baked into the deployed pod.

<details>
<summary><strong>Re-deploying</strong></summary>

**Locust only.** The faster path, for when the workloads are unchanged and only
the benchmark code is:

```bash
./benchmarking/locust/deploy.sh --delete --backend substrate
./benchmarking/locust/build_and_push.sh
./benchmarking/locust/deploy.sh --deploy --backend substrate \
  --user-class runworkload \
  --workload-name glutton \
  --workload-template glutton
```

**Everything, workloads included.** Tear the whole stack down and put it back:

```bash
./benchmarking/deploy_locust.sh --delete --backend substrate
./benchmarking/deploy_locust.sh --deploy --backend substrate \
  --user-class runworkload \
  --workload-name glutton \
  --workload-template glutton
```

Use this whenever the workloads themselves change — a new or edited
ActorTemplate/SandboxTemplate, a different sandbox class or memory limit, or a
rebuilt workload image. Under `--backend substrate`, an actor image in
particular needs this path: the template pins it by digest, and only
`workloads/deploy.sh` re-resolves that digest and retakes the golden snapshot.

</details>

### 2. Forward the locust web UI

In a separate terminal, since this holds the terminal open for as long as the
UI is needed:

```bash
kubectl port-forward -n benchmarking svc/locust 8089:8089
```

### 3. Open the locust UI

With the port-forward running, open [http://localhost:8089](http://localhost:8089).

This is where the run is started and the `RunWorkloadUser` class is picked.

## Cleanup

The steps undo the deployment in reverse, and each one is independent — stop
after the first if the cluster is being kept for another run.

### 1. Delete the benchmarking stack

```bash
./benchmarking/deploy_locust.sh --delete --backend substrate   # or agent-sandbox
```

Deletes locust first, then the benchmark workloads. Stop the `kubectl
port-forward` in the other terminal as well; it does not exit on its own.
Under `--backend agent-sandbox`, the `benchmarking` namespace itself is left
in place (deleting it would also remove the locust RBAC) — `kubectl delete
namespace benchmarking` once nothing else needs it.

### 2. Uninstall the sandbox backend from the cluster

<details open>
<summary><strong>substrate</strong></summary>

```bash
./scripts/substrate/upstream.sh hack/install-ate.sh --delete-ate-system
```

Use `--delete-all` instead to remove the registered demos along with the core
system.

</details>

<details>
<summary><strong>agent-sandbox</strong></summary>

```bash
kubectl delete -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox-with-extensions.yaml
```

</details>

### 3. Delete the cluster

TBD, and only if the cluster is no longer wanted — it is created outside this
repo, so it is deleted outside it too.

## Backends

### What gets measured

Both backends drive the same `RunWorkloadUser` locust class (a Go
[boomer](https://github.com/myzhan/boomer) worker, not Python — see
[internal/benchmarking/boomer/runworkload](internal/benchmarking/boomer/runworkload)),
repeatedly provisioning a sandbox and timing how long a client waits before it
accepts work, alternating two windows per virtual user:

- **substrate**: `FirstResume` — an actor's first activation, from its
  template's golden snapshot (or the raw image, if the template has none).
  `WarmStart` — resuming that same actor's own prior **live-memory
  snapshot**, restored in place. This is the number substrate's ~100ms
  activation target is about.
- **agent-sandbox**: `ColdCreate` — create a `Sandbox` directly from a
  `SandboxTemplate`'s blueprint, bypassing any warm pool: a true cold boot.
  `PoolClaim` — create a `SandboxClaim` against a `SandboxWarmPool`, adopting
  an already-running pod: cheaper provisioning, but **not** a live-memory
  restore, so it is not the same latency class as substrate's `WarmStart`.

### Mapping between them

| substrate | agent-sandbox equivalent used here |
|---|---|
| Atespace | plain Kubernetes `Namespace` |
| `WorkerPool` (many bin-packed actor sandboxes per worker pod) | **no equivalent** — every `Sandbox` is its own Pod. Density differs fundamentally; don't compare pool-size or "actors/node" numbers across backends |
| `ActorTemplate` | `SandboxTemplate` |
| `CreateActor` + first `ResumeActor` (`FirstResume`) | `ColdCreate` — a `Sandbox` created directly, no pool |
| Per-actor `SuspendActor`/`ResumeActor` cycle (`WarmStart`, a **live-memory snapshot restore**) | `PoolClaim` — a `SandboxClaim` adopting a pre-warmed pod. **Not** a live-memory restore |
| `DeleteActor` | delete the `Sandbox` or `SandboxClaim` |
| atenet router, `X-Actor-Target: atespace/name` header | **not needed** — every agent-sandbox `Sandbox` gets its own per-pod Service and DNS name (`status.serviceFQDN`) when its template sets `service: true`, so `runworkload` addresses it directly |
| `glutton` ping/pong actor | `benchmarking/workloads/probe` (JSON instead of protobuf, otherwise the same role) |
| `openclaw` mock actor | not ported to agent-sandbox |

The closer agent-sandbox analog to substrate's live-memory `WarmStart` — GKE
Pod Snapshots' golden-pool pattern (see
[examples/podsnapshot-golden-warmpool](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/podsnapshot-golden-warmpool)
and the
[Python SDK's `PodSnapshotSandboxClient`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/clients/python/agentic-sandbox-client/k8s_agent_sandbox/gke_extensions/snapshots/README.md))
is GKE-only and not wired into this benchmark; `ColdCreate`/`PoolClaim` is the
portable subset that runs on any cluster, kind included.

### Implementation

`internal/benchmarking/boomer/runworkload` drives both backends through one
`sandboxBackend` interface (`first`/`warm`/`teardown`, plus the
`prepare`/`prepareWarm` unmeasured preconditions each backend needs — actor
registration for substrate, nothing for agent-sandbox). The window/retry/span
machinery (`runWorkloadUser.measure`, `firstInstruction`) is shared; only how
a window is provisioned differs. `--workload-name` selects an application to
run inside the sandbox (`workload.go`); each workload declares which single
backend it targets, so a `--workload-name`/`--backend` mismatch is a
configuration error rather than a silently wrong measurement.

The agent-sandbox backend's Kubernetes clients live in
`internal/benchmarking/boomer/agentsandboxutil` rather than alongside
substrate's own `internal/benchmarking/boomer/boomerutil` — this module
cannot add to a package it doesn't own. It reaches substrate's own
`internal/` packages (scaffolding this repo doesn't duplicate: OTel tracing,
Prometheus metrics, the dynconfig hot-reload, the userclass registry) only
because `go.mod`'s module path is nested under substrate's own
(`github.com/agent-substrate/substrate/agent-infra-benchmarking`) — the same
trick substrate's own tooling relies on. Building the locust image (which
embeds the Go `boomer-worker` binary offering both backends) therefore always
needs the `dependency-repos/substrate` submodule checked out, even when the
image is going to be run with `--backend agent-sandbox`.
