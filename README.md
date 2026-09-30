# agent-infra-benchmarking
Backend for benchmarks and corresponding recipes for agent infra offerings.

## Prerequisites

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
- **Docker**, with `linux/amd64` emulation available if your machine isn't
  already amd64. `benchmarking/locust/build_and_push.sh` always builds and
  pushes `linux/amd64` images, to match the GKE nodes, regardless of the
  build host's architecture.
- **`gcloud`**, authenticated via `gcloud auth application-default login`,
  plus the `gke-gcloud-auth-plugin` component
  (`gcloud components install gke-gcloud-auth-plugin`) — required for
  `kubectl`/`gcloud container clusters get-credentials` to authenticate
  against the cluster.
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

   The example ships two pools:

   - **`default`** — GKE's own system workloads and substrate's control plane.
   - **`sandboxed`** — the benchmark workers. 

   Sizing a pool: `node_count` is the size it starts at, and
   `min_node_count` / `max_node_count` bound the autoscaler around it
   (defaulting to 1 and 3). Setting min and max equal pins the pool, which is
   what the example does for `sandboxed` — a run that is being compared with
   another has to measure a fixed amount of hardware.

   `node_locations` pins a pool to the zones it lists. Leaving it empty spreads
   the pool across every zone in the region, and `node_count` is **per zone**
   on a regional cluster like this one, so an unpinned pool is three times the
   nodes and three times the bill.

3. Download the providers:

   ```bash
   terraform -chdir=infra/terraform-gke init
   ```

4. Create everything, answering `yes` at the plan. A GKE cluster takes several
   minutes, and this is billable:

   ```bash
   terraform -chdir=infra/terraform-gke apply --var-file terraform.tfvars
   ```

## Setting up the Agent Substrate
2
1. Configure the environment

   Copy the example and fill in the values for your project:

   ```bash
   cp env.sh.example .env.sh
   ```

   > [!NOTE]
   > Optional: if you want to use `kubectl` against this cluster, configure it with:
   >
   > ```bash
   > source .env.sh
   > gcloud container clusters get-credentials $CLUSTER_NAME --location=$CLUSTER_LOCATION
   > ```

2. Install substrate into the cluster

   Upstream's installer expects to run from the substrate repo root, so invoke it
   through the wrapper:
   
   ```bash
   ./scripts/substrate/upstream.sh hack/install-ate.sh --deploy-ate-system
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

6. Collect the artifacts

   When a job ends it uploads its files to `reports/<run-id>/<test-case>/` in
   the deployment's bucket. The run id is a UTC timestamp, e.g.
   `20260929t120951z`; `--run-id` sets it instead.

   Each job leaves:

   - `locust_stats.csv`, `locust_stats_history.csv`, `locust_failures.csv`,
     `locust_exceptions.csv` — locust's own output, written as the job goes.
   - `stats.csv`, `custom_stats_history.csv`, `cluster_vitals_history.csv` —
     the job's rows read back out of Prometheus.

   The scripts download them to `reports/<run-id>/<test-case>/`, and
   [`benchmark-charts`](benchmark-charts) charts them:
   `pip install ./benchmark-charts`, then `benchmark-report` for the latest
   run, or `benchmark-report reports/<run-id>` for a given one. A drop-down in
   the window switches between the run's jobs.

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
does not outlive its pod. The reports every run uploads to
`gs://${BUCKET_NAME}/reports/<run-id>/` are unaffected, and are the record that
survives this.

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

Use terraform to destroy the cluster:

   ```bash
   terraform -chdir=infra/terraform-gke destroy --var-file terraform.tfvars
   ```
