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

```bash
./benchmarking/deploy-benchmarking.sh --deploy
```

`deploy-benchmarking.sh` deploys and runs nothing; `run_locust_job.sh` is what
measures what it deployed. One command does all three steps, in this order:

1. `locust/build_and_push.sh` builds and pushes the locust image (which carries
   the `boomer-worker` Go binary) and the openclaw actor image.
2. Prometheus goes into the `benchmarking` namespace.
3. `workloads/deploy.sh` applies the benchmark ActorTemplates and the
   WorkerPool, and waits for their golden snapshots.

None of the three is skippable. Prometheus comes before the workloads so it is
already watching while they come up, and it only ever scrapes runs that start
after it — a stack deployed without it produces runs it has no record of.

That is everything a run needs. Locust itself is not deployed here: it is a Job
per run, created by `run_locust_job.sh` below.

<details>
<summary><strong>Re-deploying</strong></summary>

Tear the whole stack down and put it back:

```bash
./benchmarking/deploy-benchmarking.sh --delete
./benchmarking/deploy-benchmarking.sh --deploy
```

Use this whenever the workloads themselves change — a new or edited
ActorTemplate, a different sandbox class or actor memory, or a rebuilt actor
image. An actor image in particular needs this path: the template pins it by
digest, and only `workloads/deploy.sh` re-resolves that digest and retakes the
golden snapshot.

Rebuilding only the benchmark code still goes through `--deploy`, since the
image is what a run pulls. That retakes the golden snapshots, so runs from
either side of it are not comparable.

</details>

## Running a benchmark as a Job

`run_locust_job.sh` is the only way to run a benchmark:

```bash
./benchmarking/run_locust_job.sh --test-case glutton-baseline
```

In order, it builds and pushes the images, resolves the case out of the image
it just built, recreates the WorkerPool, renders `locust/manifests/job.yaml`
and submits it, waits for the pod, streams the run's logs, and exits with the
Job's result — so a failed run is a non-zero exit without having to go and ask.
`--no-wait` stops after submitting instead, and leaves the run going in the
cluster.

Building on every run is what lets a change to the benchmark code or to
`tests.yaml` reach the next run without a separate deploy step. Docker's layer
cache makes it cheap when nothing changed.

### The pool is new every run

Before each Job the existing `benchmark-ateom` WorkerPool is deleted and a
fresh one applied, so no run inherits the workers the last one warmed. That is
what makes a series of runs comparable to each other rather than each one
measuring a pool that the previous run left in some state.

The ActorTemplates and their golden snapshots are untouched by this. They are
not in the pool manifest and their snapshots live in the bucket, so a run gets
cold workers without paying to re-warm what it restores from — which is why
this is safe to do per run, where redeploying the workloads is not.

The pool's shape comes from the case, not from a flag — a pool that differs is
a different run, and so is a different case. `worker-count` sizes it and
`sandbox-class` picks its runtime, defaulting to 1 and `gvisor`:

```yaml
glutton-scale:
  duration: 10m
  users: 200
  worker-count: 2      # 200 actors onto 2 workers, so queueing shows as latency
```

`sandbox-class` has to match what the ActorTemplates were deployed with: a
snapshot cannot be restored across runtimes, so actors never schedule onto a
pool of the other class. `--pool-timeout` stays a flag, since how long you are
willing to wait is not part of what a run measures.

### How the case is read

`run_locust_job.sh` resolves the case by running `testcase-helper`, a small Go
binary baked into the locust image beside the `tests.yaml` it reads:

```bash
docker run --rm --entrypoint /app/testcase-helper "${LOCUST_IMAGE}" --case glutton-scale
# {"duration":"10m","users":200,"worker-count":2,...}
```

That keeps a YAML parser off the host — the machine starting a run needs
`kubectl`, `docker` and `jq`, not PyYAML or yq — and it fails an unknown case
name before the pool is rebuilt and the Job submitted, rather than in the pod
once both have happened.

It also means the host reads **the same file the pod will**, because both read
the copy baked into the image — and since the build runs first, that copy is
the one in the working tree. Editing a case is therefore enough on its own; no
separate deploy step stands between the edit and the next run.

`worker-count` and `sandbox-class` are the host's keys, so `runner.py` drops
them before the rest of the case becomes worker flags — `boomer-worker` has no
such flags, and an unknown one is fatal to it.

### The load is in the case

`duration` and `users` are required keys of a case and `spawn-rate` defaults to
`users`, which starts the whole load at once. That default matters: locust's own
is one user a second, so a 100-user run would spend its first 100s climbing and
report the samples taken on the way up in the same table as the ones at full
load. A case meant to measure the ramp rather than a fixed load sets the rate
deliberately:

```yaml
glutton-ramp:
  duration: 10m
  users: 100
  spawn-rate: 5
```

Starting everything at once means the actors are created at once too, so a pool
that cannot take `users` activations concurrently reports the queueing as
latency. That is a real measurement of a cold pool, and it is not the same
number as the steady-state one — which is the reason a case holds the rate
rather than a flag, so every run under a name is the same run.

The Job assumes the ActorTemplates are already deployed, because a series of
runs has to measure one unchanging set of them — redeploying retakes the golden
snapshots, and runs either side of that are not comparable. Deploying is
therefore a different script, which runs nothing:

```bash
./benchmarking/deploy-benchmarking.sh --deploy
```

Prometheus comes up as part of that, and every run started after it is scraped
— which is the reason it belongs in the setup step rather than alongside a run.
Prometheus pulls, so a run that has already finished cannot be collected
retroactively. Grafana is not deployed; it sits in
`benchmarking/monitoring/grafana.yaml` for when it is wanted.

```bash
kubectl port-forward -n benchmarking svc/prometheus 9090:9090
```

Every run after it measures that same deployment, and each one blocks until it
is over, so a series can be a script:

```bash
./benchmarking/run_locust_job.sh --test-case glutton-baseline
./benchmarking/run_locust_job.sh --test-case glutton-scale
```

The two scripts take disjoint flags, so a run option cannot reach the setup
script or the other way round: either one rejects a flag it does not know.

`--deploy` takes `--worker-count`, `--sandbox-class`, `--actor-memory`,
`--otlp-endpoint` and `--wait-timeout`, all forwarded to `workloads/deploy.sh`.
`deploy-benchmarking.sh --help` lists them.

Its `--worker-count` only sizes the pool that warms the golden snapshots, since
every run replaces that pool with the one its case asks for. `--sandbox-class`
is the one to get right here: it fixes the class of the ActorTemplates, which
is immutable, and a case whose `sandbox-class` disagrees with it will deploy a
pool no actor can schedule onto.

There is no web UI and no port-forward for a run. The duration and the user
count come from the case, and the results are the pod's logs: locust prints its
aggregated table when the run ends, and the per-span lines from the worker are
in the same stream. That is what gets streamed above, and a finished Job keeps
it for a day:

```bash
kubectl logs job/locust-job -n benchmarking
```

That table is the result of a run. Prometheus holds the same measurements as a
time series instead — the worker's `/metrics` on port 8001, scraped every 10s
under the `boomer-worker` job, and kept after the pod is gone. It is the only
live endpoint the pod has: the port 8000 the locust container declares belongs
to `common/metrics.py`, which this repo does not ship.

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

  SCALE                     ACTORS
  active             avg    31.7
  active             peak   50.0
  suspended          avg    14.2
  suspended          peak   23.0
```

The scale rows are the high-water mark and the average over the run, not the
value at the end of it: `boomer_active_actors` and `boomer_suspended_actors`
are gauges, and teardown returns both to zero before the worker exits. The
average covers the whole window, so the ramp up pulls it below the level the
run settled at. Both are missing for a run of another user class, and for one
made by a worker image older than the gauges.

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
rather than from the metric.

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
little longer than the case's `duration` to finish. `runner.py` waits for both,
and exits with locust's status, or the worker's if locust was content.

A run where some requests failed is a failed Job, because that is locust's
default. Pass `--exit-code-on-error 0` through to locust when failures are an
expected part of what is being measured.

</details>

## Cleanup

The steps undo the deployment in reverse, and each one is independent — stop
after the first if the cluster is being kept for another run.

### 1. Delete the benchmarking stack

```bash
./benchmarking/deploy-benchmarking.sh --delete
```

Deletes the benchmark workloads, then Prometheus — the mirror of what
`--deploy` put there, and as all-or-nothing as that is.

**Prometheus goes with it, and so does every run it recorded.** Its storage
does not outlive its pod, so a run worth keeping should be saved before this —
from the Job's log, or from `--reports` on the run itself.

A Job is left alone, so a finished run keeps its log for its remaining day.
`./benchmarking/run_locust_job.sh --delete` removes one, and a run still in
flight should go before the workloads under it do.

### 2. Uninstall substrate from the cluster

```bash
./scripts/substrate/upstream.sh hack/install-ate.sh --delete-ate-system
```

Use `--delete-all` instead to remove the registered demos along with the core
system.

### 3. Delete the cluster

TBD, and only if the cluster is no longer wanted — it is created outside this
repo, so it is deleted outside it too.
