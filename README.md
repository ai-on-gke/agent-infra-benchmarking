# agent-infra-benchmarking
Backend for benchmarks and corresponding recipes for agent infra offerings.

## Preparation

### 1. Create a cluster

TBD. The cluster has to exist before any of the steps below; this repo does not
create one.

### 2. Configure the environment

Copy the example and fill in the values for your project:

```bash
cp env.sh.example .env.sh
```

### 3. Install substrate into the cluster

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

## Deploying the benchmarking

### 1. Deploy the locust stack

```bash
./benchmarking/deploy_locust.sh --deploy \
  --user-class runworkload \
  --workload-name glutton \
  --workload-template glutton
```

#### Options

- **`--user-class NAME`** (default: `runworkload`) — the locust user class to
  run. It has to name a `benchmarking/locust/tests/NAME.py`, and this repo ships
  only `runworkload.py`, so `runworkload` is the sole value today. The worker
  binary also registers upstream's `glutton` and `durdir` classes, but their
  locust files live in substrate rather than here.

- **`--workload-name NAME`** (default: `glutton`) — the workload a
  `RunWorkloadUser` drives, meaning what counts as an instruction the sandbox
  demonstrably ran:
  - `glutton` — POSTs a protobuf ping to `/ping` and verifies the echoed nonce.
  - `openclaw` — POSTs a fast-path prompt to `/invoke` and checks the reply.

- **`--workload-template NAME`** (default: `glutton`) — the ActorTemplate the
  workload runs on:
  - `glutton`
  - `openclaw`

This runs three stages in order: `locust/build_and_push.sh` builds and pushes
the locust image (which carries the `boomer-worker` Go binary) and the openclaw
actor image, `workloads/deploy.sh` applies the benchmark ActorTemplates and the
WorkerPool and waits for their golden snapshots, and `locust/deploy.sh` deploys
locust into the `benchmarking` namespace.

What gets measured is chosen here rather than in the web UI, because the three
values below are baked into the deployed pod. The values above are the defaults
spelled out, so `--deploy` on its own does the same thing; pass them to measure
anything else.

<details>
<summary><strong>Re-deploying</strong></summary>

**Everything, workloads included.** Tear the whole stack down and put it back:

```bash
./benchmarking/deploy_locust.sh --delete
./benchmarking/deploy_locust.sh --deploy \
  --user-class runworkload \
  --workload-name glutton \
  --workload-template glutton
```

Use this whenever the workloads themselves change — a new or edited
ActorTemplate, a different sandbox class or actor memory, or a rebuilt actor
image. An actor image in particular needs this path: the template pins it by
digest, and only `workloads/deploy.sh` re-resolves that digest and retakes the
golden snapshot.

**Locust only.** Faster, for when the workloads are unchanged and only the
benchmark code is:

```bash
./benchmarking/locust/deploy.sh --delete
./benchmarking/locust/build_and_push.sh
./benchmarking/locust/deploy.sh --deploy \
  --user-class runworkload \
  --workload-name glutton \
  --workload-template glutton
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

The steps undo the deployment in reverse, and each one is independent — stop
after the first if the cluster is being kept for another run.

### 1. Delete the benchmarking stack

```bash
./benchmarking/deploy_locust.sh --delete
```

Deletes locust first, then the benchmark workloads. Stop the `kubectl
port-forward` in the other terminal as well; it does not exit on its own.

### 2. Uninstall substrate from the cluster

```bash
./scripts/substrate/upstream.sh hack/install-ate.sh --delete-ate-system
```

Use `--delete-all` instead to remove the registered demos along with the core
system.

### 3. Delete the cluster

TBD, and only if the cluster is no longer wanted — it is created outside this
repo, so it is deleted outside it too.
