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
./benchmarking/deploy_locust.sh --deploy
```

This runs three stages in order: `workloads/deploy.sh` applies the benchmark
ActorTemplates and the WorkerPool and waits for their golden snapshots,
`locust/build_and_push.sh` builds and pushes the locust image (which carries the
`boomer-worker` Go binary), and `locust/deploy.sh` deploys locust into the
`benchmarking` namespace.


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
