# agent-infra-benchmarking
Backend for benchmarks and corresponding recipes for agent infra offerings.

## Prerequisites

- **Terraform** (1.5+) — stands up the cluster, bucket and registry from
  [`infra/terraform-gke`](infra/terraform-gke). A `.terraform.lock.hcl` is
  committed, so `terraform init` resolves the pinned provider versions.
- **Go 1.27+** — matches [`go.mod`](go.mod); also builds `kubectl-ate` below.
- **`kubectl`**, recent enough to work with the ClusterTrustBundle/PodCertificateRequest
  APIs substrate depends on (GKE 1.36+ serves them; kubectl 1.31 or newer is
  what's been tested against this repo).
- **`kubectl-ate`**, substrate's own kubectl plugin, on `PATH`. Built from the
  submodule (see step 1 below):
  ```bash
  (cd dependency-repos/substrate && go install ./cmd/kubectl-ate)
  ```
- **`jq`** and **`envsubst`** (the `gettext` package) — used throughout
  `benchmarking/*.sh` and `.terraform-env.sh`.
- **Docker**, with Buildx (`docker buildx version`) and `linux/amd64`
  emulation available if your machine isn't already amd64 — e.g.
  `docker run --privileged --rm tonistiigi/binfmt --install amd64` registers
  the QEMU handler. `benchmarking/locust/build_and_push.sh` always builds and
  pushes `linux/amd64` images, to match the GKE nodes, regardless of the
  build host's architecture.
- **`gcloud`**, authenticated two ways, because the two are read by different
  callers:
  - `gcloud auth login` — for the `gcloud` CLI itself, which the scripts call
    to get an Artifact Registry token, read the project number, and fetch
    cluster credentials.
  - `gcloud auth application-default login` — the application-default
    credentials Terraform and the Kubernetes client libraries read.

  Plus the `gke-gcloud-auth-plugin` component
  (`gcloud components install gke-gcloud-auth-plugin`), required for
  `kubectl`/`gcloud container clusters get-credentials` to authenticate
  against the cluster.
- **Python 3.9+** with `pip` — for [`benchmark-charts`](benchmark-charts),
  which plots a downloaded run (`pip install ./benchmark-charts`).
- A GCP project with these APIs enabled: `compute.googleapis.com`,
  `container.googleapis.com`, `artifactregistry.googleapis.com`,
  `storage.googleapis.com`, `iam.googleapis.com`,
  `cloudresourcemanager.googleapis.com`.
- An operator account able to create and manage VPC networks and firewall
  rules, GKE clusters and node pools, Artifact Registry repositories, GCS
  buckets, service accounts and project-level IAM bindings — `Owner` or
  `Editor` covers all of it; see the `enable_atelet_iam`/
  `enable_atelet_bucket_iam` comments in
  [`infra/terraform-gke/variables.tf`](infra/terraform-gke/variables.tf) if
  you're granting narrower roles instead.
- **Cost**: this stands up billable resources — a GKE Standard cluster with
  autoscaling node pools, a VPC, a GCS bucket and an Artifact Registry repo.
  See [Cleanup](#cleanup) to tear it down.

## Setting up the infrastructure

The cluster, the snapshot bucket and the Artifact Registry repository come from
the Terraform config in `infra/terraform-gke`. Run from the repo root, with
gcloud application-default credentials already in place.

1. Initialize the `substrate` submodule. Terraform reads its build version
   off this checkout (`infra/terraform-gke/substrate-version.sh`), and every
   script under `scripts/substrate` shells out into it, so both fail without
   it:

   ```bash
   git submodule update --init dependency-repos/substrate
   ```

2. Copy the example — the real file is gitignored and per-developer:

   ```bash
   cp infra/terraform-gke/terraform.tfvars.example infra/terraform-gke/terraform.tfvars
   ```

   Then set in it:

   - `project_id` — required, no default.
   - `default_resource_name` — default name for all created resources.
   - `cluster.node_pools` — the hardware the benchmark runs on, see below.

   The example ships three pools:

   - **`default`** — GKE's own system workloads and substrate's control plane.
   - **`sandboxed`** — the gVisor benchmark workers.
   - **`microvm`** — micro-VM workers, only needed to run the `microvm`
     sandbox class. Drop it if you only run gVisor.

   Sizing a pool: `node_count` is the size it starts at, and
   `min_node_count` / `max_node_count` bound the autoscaler around it
   (defaulting to 1 and 3). Setting min and max equal pins the pool, which is
   what the example does for every pool - a run that is being compared with
   another has to measure a fixed amount of hardware.

   `node_locations` pins a pool to the zones it lists. Leaving it empty spreads
   the pool across every zone in the region, and `node_count` is **per zone**
   on a regional cluster like this one, so an unpinned pool is three times the
   nodes and three times the bill.

   > [!IMPORTANT]
   > **The shipped sizes are large.** The example reproduces the published
   > glutton run, which needs 100 gVisor workers spread thin: the `sandboxed`
   > pool is **30 × `c2-standard-16`**, plus 1 for `default` — **31 nodes, all
   > pinned to `us-central1-a`, ≈ 496 vCPUs in one zone** (the optional
   > `microvm` pool adds one more node, ≈ 512 vCPUs). That needs regional C2
   > CPU quota to match and enough single-zone capacity to schedule, or the
   > apply stalls on quota and the pods never leave `Pending`. Shrink
   > `sandboxed`'s `node_count`/`min`/`max` (and the case's `worker-count`) for
   > a smaller, cheaper run; the numbers will differ from the published ones.

   The control plane and every node pool are pinned to a fixed GKE version
   with no release channel, so GKE holds them rather than auto-upgrading a run
   out from under a comparison. A specific patch version is retired over time,
   so if `apply` reports an unsupported version, update `kubernetes_version`
   (and each pool's `version`) to a currently-served one — see the comment in
   [`terraform.tfvars.example`](infra/terraform-gke/terraform.tfvars.example).

3. Initialize Terraform. The state can be kept in one of two places:

   - **Local** (default): nothing to set up. Only this machine can manage the
     deployment, though, and losing the state file leaves its resources
     orphaned.
   - **GCS bucket**: anyone on the project can manage the deployment, and the
     state is versioned and outlives the machine. The bucket has to be created
     once per project first.

   Follow one of:

   1. **Local state**, in `infra/terraform-gke/terraform.tfstate`:

      ```bash
      terraform -chdir=infra/terraform-gke init
      ```

   2. **State in GCS.** The bucket is shared by the project's deployments; the
      prefix keeps each one's state apart, so use your `default_resource_name`.
      `backend_override.tf` is gitignored:

      ```bash
      # Once per project, if it does not exist yet:
      gcloud storage buckets create gs://<project-id>-tfstate --location=us-central1 \
        --uniform-bucket-level-access
      gcloud storage buckets update gs://<project-id>-tfstate --versioning

      cat > infra/terraform-gke/backend_override.tf <<'EOF'
      terraform {
        backend "gcs" {}
      }
      EOF

      terraform -chdir=infra/terraform-gke init \
        -backend-config=bucket=<project-id>-tfstate \
        -backend-config=prefix=<default_resource_name>
      ```

      `init` offers to copy an existing local state into the bucket.

4. Create everything, answering `yes` at the plan. A GKE cluster takes several
   minutes, and this is billable:

   ```bash
   terraform -chdir=infra/terraform-gke apply
   ```

## Setting up the Agent Substrate

1. Configure the environment

   Copy the example. There is nothing to fill in for a cluster made by the
   Terraform above: `.env.sh` reads the project, cluster, bucket and registry
   from its state.

   ```bash
   cp env.sh.example .env.sh
   ```

   > [!NOTE]
   > Already have a `.env.sh` from an earlier checkout? Re-copy it.
   > `env.sh.example` and the `.terraform-env.sh` it sources are versioned and
   > do change — a stale `.env.sh` silently keeps the old behaviour.

   Then point `kubectl` at the cluster. This is **not** optional: every step
   below — installing substrate, deploying the benchmarking stack, running a
   job — talks to the cluster through `kubectl`, and none of them works until
   this has run.

   ```bash
   source .env.sh
   gcloud container clusters get-credentials "$CLUSTER_NAME" --location="$CLUSTER_LOCATION"
   ```

2. Install substrate into the cluster

   Upstream's installer expects to run from the substrate repo root, so invoke it
   through the wrapper:
   
   ```bash
   ./scripts/substrate/upstream.sh hack/install-ate.sh --deploy-ate-system
   ```

   That installer only lays down the default gVisor SandboxConfig. To also run
   the `microvm` sandbox class (`--sandbox-class microvm`), install the micro-VM
   dependencies: the cluster-wide `microvm` SandboxConfig and its kata +
   cloud-hypervisor assets. This must run *after* the step above (it needs
   substrate's CRDs), and the actor guests boot on `/dev/kvm`, so the worker
   nodes need nested virtualization — a Terraform node pool with
   `enable_nested_virtualization = true` on a non-E2 machine type (the `microvm`
   pool in [`terraform.tfvars.example`](infra/terraform-gke/terraform.tfvars.example)).

   ```bash
   ./scripts/substrate/upstream.sh hack/install-microvm-deps.sh --install
   ```

3. Deploying the benchmarking

   ```bash
   ./benchmarking/deploy-benchmarking.sh --deploy
   ```

4. Pick the test case to run

   A test case is a named entry in [`benchmarking/tests.yaml`](benchmarking/tests.yaml),
   under `cases:`. The key is what the next step passes to `--test-case`.

5. Run a benchmark job script.

   ```bash
   ./benchmarking/run_locust_job.sh --test-case glutton --follow-logs
   ```

   A run can hold several jobs, one per test case. To run every case in
   `tests.yaml` one after another under a single run id:

   ```bash
   ./benchmarking/run_all_test_cases.sh
   ```

   Substrate blocks all outbound traffic from an actor that has no egress
   policy. The worker gives each actor one that allows only the hosts named by
   URLs in its ActorTemplate's env, such as the OTLP collector and openclaw's
   mock LLM. To let actors reach another service, add its URL to the template.

6. Collect the artifacts

   When a job ends it uploads its files to `reports/<run-id>/<test-case>/` in
   the deployment's bucket. The run id is a UTC timestamp, e.g.
   `20260929t155627z`; `--run-id` sets it instead.

   Each job leaves:

   - `locust_stats.csv`, `locust_stats_history.csv`, `locust_failures.csv`,
     `locust_exceptions.csv` — locust's own output, written as the job goes.
   - `stats.csv`, `custom_stats_history.csv`, `cluster_vitals_history.csv` —
     the job's rows read back out of Prometheus.
   - `run.json` - the case, how the run went, every image it ran by digest,
     the commits they were built from, the report bucket, and the
     `terraform.tfvars` describing the hardware the run measured. The locust
     image is also tagged with the run id in the registry.

   The scripts download them to `reports/<run-id>/<test-case>/`, and
   [`benchmark-charts`](benchmark-charts) charts them:
   `pip install ./benchmark-charts`, then `benchmark-report` for the latest
   run, or `benchmark-report reports/<run-id>` for a given one. A drop-down in
   the window switches between the run's jobs.

## Reading the results

A committed example run is in
[`reports/20260929t155627z/`](reports/20260929t155627z/) — its
[README](reports/20260929t155627z/README.md) is the worked example for
everything below.

### What each row measures

The worker drives an actor through `resume → one instruction → suspend` and
times the pieces. The request names in the CSVs are:

| Name | What it times |
| --- | --- |
| `TTFI` | Time to first instruction: from the start of the explicit `ResumeActor` to the moment the instruction is handed off. It is the resume latency the client waited through; it does **not** include serving the instruction (that is the application's cost, not substrate's), so under `resume-mode: implicit` it is near zero and `ExecutionTime` is the number to read instead. |
| `CycleTime` | The whole `resume → instruction → suspend` cycle: `TTFI` + the instruction's service time + the suspend. |
| `Resume` / `Suspend` | The `ResumeActor` / `SuspendActor` gRPC calls on their own. |
| `ExecutionTime` | The instruction's own round trip to the actor. |
| `CreateActor` / `CreateAtespace` / `DeleteActor` | The lifecycle gRPC calls. `CreateAtespace` happens once per run; `DeleteActor` is teardown. |
| `ActiveActors` / `IdleActors` | Gauges in `custom_stats_history.csv`: actors currently resumed vs. suspended. |

### Which file is authoritative

`locust_stats.csv` / `locust_stats_history.csv` are the numbers to quote.
locust records response times at 1 ms resolution, so its percentiles are exact
rather than snapped to a bucket bound, and they include failed requests, not
just successes.

`stats.csv` is the **Prometheus mirror** — the same run read back out of the
worker's histogram so a record survives the pod. That histogram uses
`ExponentialBuckets(1, 2, 16)` (1, 2, 4, … 32768 ms) and counts successes
only, so its tail percentiles are interpolated within power-of-two buckets and
are coarse: read it to confirm the shape, not for an exact p99. Where the two
disagree in the tail, `locust_stats.csv` wins.

### The ramp, steady state, and the p99 stall

Two things distort a naive read of the aggregate percentiles, and both show up
in the example run:

- **The ramp is in the aggregate.** `glutton` ramps to 100 users at
  `spawn-rate: 2`, so the first ~50 s climb through every lower concurrency.
  The aggregate row averages that in. Read steady state from the per-second
  history after the ramp, not from the single aggregate row.
- **A few multi-second stalls carry the tail.** In the example the p50 is flat
  at ~230 ms the whole run, but a handful of stalls (reaching 9–13 s) land in
  scattered seconds and push p99.9/max up with them; across repeats the same
  stall shows at ~6–9 s. That is what a ~1 s p99 is made of — not a uniformly
  slower run. **Attribute the stall before publishing a p99.** The leading
  suspect is node-level I/O pressure on the worker nodes; a p50 headline plus a
  named, understood tail is honest, a bare p99 is not.

### Repeats

One run is one sample. For a number worth comparing, deploy the stack once and
run the same case several times under distinct `--run-id`s (cold workers each
time, since `run_locust_job.sh` recreates the pool per run), then report the
min / median / max across repeats rather than a single run's figure. The
example run is a single sample and is labelled as such.

## Cleanup

The steps undo the deployment in reverse, and each one is independent — stop
after the first if the cluster is being kept for another run.

### 1. Delete the benchmarking stack

```bash
./benchmarking/deploy-benchmarking.sh --delete
```

Deletes the benchmark workloads, then Prometheus — the mirror of what
`--deploy` put there, and as all-or-nothing as that is.

**Prometheus goes with it, and so does every run it recorded.** Its data is on
a volume that survives a restart of its pod, and is deleted here along with it.
The reports every run uploads to
`gs://${BUCKET_NAME}/reports/<run-id>/` are unaffected, and are the record that
survives this.

This takes the `benchmarking` namespace with it — Prometheus's manifest owns
that namespace, and the benchmark Job runs in it — so any Job there goes too,
and its log with it. Grab anything you still want from `kubectl logs` first.
(On its own, a finished Job keeps its log for a day on its
`ttlSecondsAfterFinished`; this teardown is what cuts that short.) A run still
in flight should be stopped first with
`./benchmarking/run_locust_job.sh --delete`, so its actors are cleaned up
before the workers under them are torn down.

### 2. Uninstall substrate from the cluster

```bash
./scripts/substrate/upstream.sh hack/install-ate.sh --delete-ate-system
```

Use `--delete-all` instead to remove the registered demos along with the core
system.

### 3. Delete the cluster

Use terraform to destroy the cluster. **This also deletes the bucket and every
run's reports in it**, since the example sets `bucket_force_destroy = true`.
Download any worth keeping first, or set it to `false`, in which case destroy
stops at the bucket while it holds anything.

   ```bash
   terraform -chdir=infra/terraform-gke destroy
   ```
