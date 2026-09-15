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

Pass `--skip-build` to reuse the image already pushed, and `--delete` to tear
the whole stack down in reverse order. `--help` lists the rest.
