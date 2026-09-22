# agent-infra-benchmarking
Backend for benchmarks and corresponding recipes for agent infra offerings.

## Setting up the infrastructure

The clusters, the snapshot bucket and the Artifact Registry repository come from
the Terraform config in `infra/terraform-gke`. Run from the repo root, with
gcloud application-default credentials already in place.

1. Copy the example — the real file is gitignored and per-developer:

   ```bash
   cp infra/terraform-gke/terraform.tfvars.example infra/terraform-gke/terraform.tfvars
   ```

   Then set in it:

   - `project_id` — required, no default.
   - `default_resource_name` — required, no default. Names the bucket and the
     repository, and prefixes every cluster: `<default_resource_name>-<key>`.
   - `clusters` — a Standard cluster with a `sandbox_gvisor = true` pool, plus an
     ordinary pool for GKE's own system workloads.
   - `bucket_name` — has to match `BUCKET_NAME` in `.env.sh`. Bucket names are
     unique across all of GCP, so `default_resource_name` is often taken.
   - `enable_atelet_iam` — `true`, unless another deployment in the same project
     already owns these bindings.

2. Download the providers:

   ```bash
   terraform -chdir=infra/terraform-gke init
   ```

3. Create everything, answering `yes` at the plan. A GKE cluster takes several
   minutes, and this is billable:

   ```bash
   terraform -chdir=infra/terraform-gke apply
   ```

The outputs carry the bucket, the repository and a `get-credentials` command per
cluster:

```bash
terraform -chdir=infra/terraform-gke output get_credentials_commands
```

## Preparation

### 1. Create a cluster

Covered by [Setting up the infrastructure](#setting-up-the-infrastructure)
above. The cluster has to exist before any of the steps below.

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

**Locust only.** The faster path, for when the workloads are unchanged and only
the benchmark code is:

```bash
./benchmarking/locust/deploy.sh --delete
./benchmarking/locust/build_and_push.sh
./benchmarking/locust/deploy.sh --deploy \
  --user-class runworkload \
  --workload-name glutton \
  --workload-template glutton
```

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

## Running a benchmark as a Job

Everything above deploys locust as a Deployment: it is held open, and a run
starts and stops from the web UI. `run_locust_job.sh` is the other way to run
the same benchmark — as a Job that runs for a stated duration and then
finishes, so that its completion is something a script can wait for.

```bash
./benchmarking/run_locust_job.sh --duration 5m --users 10
```

It submits the Job, waits for the pod, streams the run's logs as they are
produced, and exits with the Job's result — so a failed run is a non-zero exit
without having to go and ask. `--no-wait` stops after submitting instead, and
leaves the run going in the cluster.

The Job assumes the workloads are already deployed, because a series of runs
has to measure one unchanging deployment — redeploying retakes the golden
snapshots, and runs either side of that are not comparable. Setting up is
therefore its own command, which runs nothing and exits when it is done:

```bash
./benchmarking/run_locust_job.sh --build --deploy-workloads --deploy-monitoring
```

`--deploy-monitoring` brings up Prometheus in the `benchmarking` namespace, and
every run started after it is scraped — which is the reason it belongs in the
setup step rather than alongside a run. Prometheus pulls, so a run that has
already finished cannot be collected retroactively. Grafana is not deployed;
it sits in `benchmarking/monitoring/grafana.yaml` for when it is wanted.

```bash
kubectl port-forward -n benchmarking svc/prometheus 9090:9090
```

Every run after it measures that same deployment, and each one blocks until it
is over, so a series can be a script:

```bash
./benchmarking/run_locust_job.sh --users 50
./benchmarking/run_locust_job.sh --users 100
```

Passing a run's options to the setup command is an error rather than something
quietly dropped, since nothing there would have run them.

`--user-class`, `--workload-name` and `--workload-template` mean what they do
in `deploy_locust.sh`, and `--deploy-workloads` takes that script's
`--worker-count`, `--sandbox-class`, `--actor-memory` and `--otlp-endpoint`
alongside it. `--help` lists them all.

There is no web UI and no port-forward for the run itself. The duration and the
user count are arguments here, where the UI's form carries them for the
Deployment, and the results are the pod's logs: locust prints its aggregated
table when the run ends, and the per-span lines from the worker are in the same
stream. That is what gets streamed above, and a finished Job keeps it for a day:

```bash
kubectl logs job/locust-job -n benchmarking
```

That table is the result of a run. Prometheus, when deployed, holds the same
measurements as a time series instead — the worker's `/metrics` on port 8001,
scraped every 10s under the `boomer-worker` job, and kept after the pod is
gone. It is the only live endpoint either pod has: the port 8000 the locust
container declares belongs to `common/metrics.py`, which this repo does not
ship.

### Telling one run from another

When the run ends, `run_locust_job.sh` queries Prometheus for what it recorded
and prints it:

```
=== Metrics for run_id=baseline-50u ===
  REQUEST            STATUS    COUNT
  FirstResume        success   40
  Instruction        success   2450
  TTFI               failure   3
  TTFI               success   2410

  REQUEST                   LATENCY (ms)
  FirstResume        p50    1620.5
  FirstResume        p95    2300.0
  TTFI               p50    91.4
  TTFI               p95    183.8
  TTFI               p99    412.0
```

It port-forwards to Prometheus on a port `kubectl` picks, so it cannot collide
with a forward already open, and closes it again afterwards. The whole summary
is best-effort and runs after the result of the run is settled: no failure in
it changes the exit status, and Prometheus not being deployed, or `jq` or
`curl` not being installed, skips it with a line saying so. `--no-metrics`
turns it off. A latency shown as `-` is a request that never succeeded, so
there is no distribution to take a quantile of.

Every run carries a `run_id`, defaulting to a UTC timestamp and settable with
`--run-id`. The Job puts it on its pod as a label, and Prometheus relabels it
onto every series the run produces, along with what the run was:

```promql
histogram_quantile(0.95,
  sum by (le, name) (
    rate(locust_request_duration_milliseconds_bucket{run_id="baseline-50u"}[1m])
  ))
```

`users`, `duration`, `workload` and `template` come across the same way, so a
series of runs can be compared without having to know which is which:

```promql
sum by (users) (rate(locust_requests_total{status="failure"}[1m]))
```

The worker's own labels — `method`, `name`, `status`, `user_class` — are fixed
upstream and say nothing about the run, which is why this comes from the pod
rather than from the metric. A run under the Deployment carries none of these
labels, since a session steered from the web UI has no fixed shape to record.

Reusing a `run_id` merges two runs into one series, so leave it to the
timestamp unless a run is worth naming.

**Prometheus keeps nothing across a restart.** Its TSDB is written to the
container's own filesystem — `monitoring/prometheus.yaml` mounts the config
and nothing else — so every stored session is lost whenever that pod goes
away: an eviction, a node drain, a change to the manifest. Sessions accumulate
for as long as one pod happens to live, which is enough for comparing a
morning's runs and is not a record to keep. The log of each Job is the durable
artifact; a run worth keeping should be saved from there while the Job is
still around. Mounting a PersistentVolumeClaim at `/prometheus/` is what
changes this, and needs `strategy: Recreate` on that Deployment to avoid an
RWO volume deadlocking the rollout.

<details>
<summary><strong>How it ends itself</strong></summary>

The Job runs one container, `/app/runner.py`, which owns both processes: a
`kind: Job` pod is finished only once every container has exited, and two peer
containers would each have to decide to stop on their own.

locust runs headless with `-t`, so the time limit stops the test and sends a
`quit` to the worker. The worker returns from its run loop on that, then
suspends and deletes every actor it created — which is why the pod can take a
little longer than `--duration` to finish. `runner.py` waits for both, and
exits with locust's status, or the worker's if locust was content.

A run where some requests failed is a failed Job, because that is locust's
default. Pass `--exit-code-on-error 0` through to locust when failures are an
expected part of what is being measured.

</details>

## Cleanup

The steps undo the deployment in reverse, and each one is independent — stop
after the first if the cluster is being kept for another run.

### 1. Delete the benchmarking stack

```bash
./benchmarking/deploy_locust.sh --delete
```

Deletes locust first, then the benchmark workloads. Stop the `kubectl
port-forward` in the other terminal as well; it does not exit on its own.

This deletes the Deployment, not the Job — a finished Job is kept for a day so
its logs outlive it. `./benchmarking/run_locust_job.sh --delete` removes it
sooner, and leaves the workloads alone.

### 2. Uninstall substrate from the cluster

```bash
./scripts/substrate/upstream.sh hack/install-ate.sh --delete-ate-system
```

Use `--delete-all` instead to remove the registered demos along with the core
system.

### 3. Delete the cluster

TBD, and only if the cluster is no longer wanted — it is created outside this
repo, so it is deleted outside it too.
