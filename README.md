# agent-infra-benchmarking

Backend for benchmarks and corresponding recipes for agent infra offerings.

This fork replaces [substrate](https://github.com/agent-substrate/substrate)
as the sandbox backend with
[agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
(`sigs.k8s.io/agent-sandbox`). See [Mapping from substrate](#mapping-from-substrate)
for what changed and why, and [Known limitations](#known-limitations) for
what this port does not (yet) cover.

## What gets measured

A `RunWorkloadUser` locust class (driven by a Go
[boomer](https://github.com/myzhan/boomer) worker, not Python — see
[internal/benchmarking/boomer/runworkload](internal/benchmarking/boomer/runworkload))
repeatedly provisions a sandbox and times how long a client waits before it
accepts work. Two windows are measured per virtual user, alternating:

- **ColdCreate** — create a `Sandbox` directly from a `SandboxTemplate`'s
  blueprint, bypassing any warm pool. Image pull, pod scheduling, and
  application start all happen inside the window: a true cold boot.
- **PoolClaim** — create a `SandboxClaim` against a `SandboxWarmPool`.
  Adoption is a label flip onto an already-running pod, so this is the "warm"
  number to compare `ColdCreate` against.

Each window POSTs a nonce to the sandbox's `/ping` route and retries until the
echo comes back correct (see
[benchmarking/workloads/probe](benchmarking/workloads/probe) for the target —
a minimal HTTP probe, not a real agent, so the benchmark measures
agent-sandbox's activation path rather than any application's own startup
cost).

## Preparation

### 1. Create a cluster

TBD. The cluster has to exist before any of the steps below; this repo does
not create one. `infra/terraform-gke` has example Terraform, or use a local
`kind` cluster for the portable (`ColdCreate`/`PoolClaim`) flow this README
covers.

### 2. Configure the environment

Copy the example and fill in the values for your project:

```bash
cp env.sh.example .env.sh
```

### 3. Install agent-sandbox

```bash
export VERSION=v1.0.3   # see https://github.com/kubernetes-sigs/agent-sandbox/releases
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox-with-extensions.yaml
kubectl -n agent-sandbox-system rollout status deployment --timeout=180s
```

This installs both the core `Sandbox` CRD/controller and the extensions
(`SandboxTemplate`, `SandboxWarmPool`, `SandboxClaim`) this benchmark needs.
See the [agent-sandbox README](https://github.com/kubernetes-sigs/agent-sandbox#installation)
for selective-install and Helm options.

## Deploying the benchmarking

### 1. Deploy the locust stack

```bash
./benchmarking/deploy_locust.sh --deploy
```

This runs three stages in order: `locust/build_and_push.sh` builds and pushes
the locust image (which carries the `boomer-worker` Go binary) and the ping
probe image, `workloads/deploy.sh` applies the `benchmarking` namespace, the
`ping` `SandboxTemplate`, and its `SandboxWarmPool`, waiting for the pool to
fill, and `locust/deploy.sh` deploys locust (master + boomer worker, with the
RBAC to create/delete `Sandbox`/`SandboxClaim` objects in that namespace)
into the same namespace.

#### Options

- **`--worker-count N`** (default: `1`) — `SandboxWarmPool` replicas.
- **`--runtime-class NAME`** (default: unset, i.e. `runc`) — the `RuntimeClass`
  the probe `SandboxTemplate` requests. Set to `gvisor` or another
  cluster-registered class for stronger isolation; must already exist
  (`kubectl get runtimeclass`).
- **`--probe-memory SIZE`** (default: `64Mi`) — memory limit for the probe
  container.
- **`--workload-name NAME`** (default: `ping`) — the workload a
  `RunWorkloadUser` drives; `ping` is the only one this fork ships (see
  [Known limitations](#known-limitations)).
- **`--workload-template NAME`** (default: `ping`) — the `SandboxTemplate`
  the workload runs on.
- **`--skip-build`** — reuse existing `:latest` images instead of rebuilding.

<details>
<summary><strong>Re-deploying</strong></summary>

**Locust only.** The faster path, for when the workloads are unchanged and
only the benchmark code is:

```bash
./benchmarking/locust/deploy.sh --delete
./benchmarking/locust/build_and_push.sh
./benchmarking/locust/deploy.sh --deploy --user-class runworkload --workload-name ping --workload-template ping
```

**Everything, workloads included.** Tear the whole stack down and put it back
— use this whenever the `SandboxTemplate` itself changes (a different probe
image, memory limit, or runtime class):

```bash
./benchmarking/deploy_locust.sh --delete
./benchmarking/deploy_locust.sh --deploy
```

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

```bash
./benchmarking/deploy_locust.sh --delete
```

Deletes locust first, then the benchmark workloads (namespace excluded — see
the comment in `benchmarking/workloads/deploy.sh`; delete it by hand with
`kubectl delete namespace benchmarking` once nothing else needs it). Stop the
`kubectl port-forward` in the other terminal as well; it does not exit on its
own.

## Mapping from substrate

| substrate | agent-sandbox equivalent used here |
|---|---|
| Atespace | plain Kubernetes `Namespace` |
| `WorkerPool` (many bin-packed actor sandboxes per worker pod) | **no equivalent** — every `Sandbox` is its own Pod. Density differs fundamentally; see [Known limitations](#known-limitations) |
| `ActorTemplate` | `SandboxTemplate` |
| `CreateActor` + first `ResumeActor` (substrate's `FirstResume`) | `ColdCreate` — a `Sandbox` created directly, no pool |
| Per-actor `SuspendActor`/`ResumeActor` cycle (substrate's `WarmStart`, a **live-memory snapshot restore**) | `PoolClaim` — a `SandboxClaim` adopting a pre-warmed pod. **Not a live-memory restore** — see below |
| `DeleteActor` | delete the `Sandbox` or `SandboxClaim` |
| atenet router, `X-Actor-Target: atespace/name` header | **not needed** — every agent-sandbox `Sandbox` gets its own per-pod Service and DNS name (`status.serviceFQDN`) when its template sets `service: true`, so `runworkload` addresses it directly |
| `glutton` ping/pong actor | `benchmarking/workloads/probe` (JSON instead of protobuf, otherwise the same role) |
| `openclaw` mock actor | not ported (see [Known limitations](#known-limitations)) |

**`PoolClaim` is not substrate's `WarmStart`.** substrate's warm path restores
one actor's own prior live memory and filesystem state from a snapshot — the
number its ~100ms activation target is about. agent-sandbox's `SandboxClaim`
adoption is cheaper provisioning (a label flip onto an already-running,
already-imaged pod), not a live-process restore. The closer analog —
live-memory snapshot/restore via GKE Pod Snapshots — exists in agent-sandbox
(see
[examples/podsnapshot-golden-warmpool](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/podsnapshot-golden-warmpool)
and the [Python SDK's `PodSnapshotSandboxClient`](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/clients/python/agentic-sandbox-client/k8s_agent_sandbox/gke_extensions/snapshots/README.md))
but is GKE-only and not wired into this benchmark — see below.

## Known limitations

- **No live-memory snapshot/warm-start.** `PoolClaim` times provisioning
  from a warm pool, not a live-memory restore. Wiring GKE Pod Snapshots'
  golden-pool pattern into a third measurement window is tracked as future
  work; it would only run on GKE, unlike the portable `ColdCreate`/
  `PoolClaim` pair this README covers.
- **Density model differs from substrate's.** substrate bin-packs many actor
  sandboxes into each `WorkerPool` worker pod; agent-sandbox is one
  Kubernetes Pod per `Sandbox`. Don't compare "actors/node" or raw pool-size
  numbers between the two backends — compare activation latency only.
- **Only the `ping` workload is ported.** substrate's `openclaw` (a headless
  coding-agent container hit on its canned fast path) is not — nothing in
  this fork depends on `demos/openclaw`, which is unused dead weight now.
  Adding a second workload means implementing the `workload` interface in
  `internal/benchmarking/boomer/runworkload` and a matching `SandboxTemplate`.
- **Substrate-specific `dynconfig` knobs are gone.** `resume_mode`,
  `lifecycle_mode`, `durdir_*`, and `mem_target`/`mem_churn`/`mem_read` don't
  apply to this backend and were dropped; only `trace_probability`,
  `min_wait_time`, and `max_wait_time` remain tunable from the locust web UI.
