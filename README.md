# agent-infra-benchmarking
Backend for benchmarks and corresponding recipes for agent infra offerings.

## Prerequisites

- **Go 1.27+** — matches [`go.mod`](benchmarking/locust/go/go.mod); also builds `kubectl-ate` below.
- **`kubectl`**, recent enough to work with the ClusterTrustBundle/PodCertificateRequest
  APIs substrate depends on (GKE 1.36+ serves them; kubectl 1.31 or newer is
  what's been tested against this repo).
- **`kubectl-ate`**, substrate's own kubectl plugin, on `PATH`. Built from the
  submodule in step 3 of
  [Setting up the Agent Substrate](#setting-up-the-agent-substrate).
- **`jq`** and **`envsubst`** (the `gettext` package) — used throughout
  `benchmarking/*.sh` and `.terraform-env.sh`.
- **Docker**, with `linux/amd64` emulation available if your machine isn't
  already amd64. `benchmarking/images/lib.sh` always builds and
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
  [`benchmarking/automation/infra/common/variables.tf`](benchmarking/automation/infra/common/variables.tf)
  if you're granting narrower roles instead.
- **Cost**: this stands up billable resources — a GKE Standard cluster with
  autoscaling node pools, a VPC, a GCS bucket and an Artifact Registry repo.
  See [Cleanup](#cleanup) to tear it down.

## Setting up the infrastructure

The cluster, the snapshot bucket and the Artifact Registry repository come from
the Terraform config in `benchmarking/automation/infra/common`. Run from the
repo root, with gcloud application-default credentials already in place.

1. Initialize the `substrate` submodule. Terraform reads its build version
   off this checkout
   (`benchmarking/automation/infra/common/substrate-version.sh`), and every
   script under `scripts/substrate` shells out into it, so both fail without
   it:

   ```bash
   git submodule update --init dependency-repos/substrate
   ```

2. Copy the example — the real file is gitignored and per-developer:

   ```bash
   cp benchmarking/automation/infra/common/terraform.tfvars.example benchmarking/automation/infra/common/terraform.tfvars
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
   what the example does for both pools - a run that is being compared with
   another has to measure a fixed amount of hardware.

   `node_locations` pins a pool to the zones it lists. Leaving it empty spreads
   the pool across every zone in the region, and `node_count` is **per zone**
   on a regional cluster like this one, so an unpinned pool is three times the
   nodes and three times the bill.

3. Initialize Terraform. The state can be kept in one of two places:

   - **Local** (default): nothing to set up. Only this machine can manage the
     deployment, though, and losing the state file leaves its resources
     orphaned.
   - **GCS bucket**: anyone on the project can manage the deployment, and the
     state is versioned and outlives the machine. The bucket has to be created
     once per project first.

   Follow one of:

   1. **Local state**, in `benchmarking/automation/infra/common/terraform.tfstate`:

      ```bash
      terraform -chdir=benchmarking/automation/infra/common init
      ```

   2. **State in GCS.** The bucket is shared by the project's deployments; the
      prefix keeps each one's state apart, so use your `default_resource_name`.
      `backend_override.tf` is gitignored:

      ```bash
      # Once per project, if it does not exist yet:
      gcloud storage buckets create gs://<project-id>-tfstate --location=us-central1 \
        --uniform-bucket-level-access
      gcloud storage buckets update gs://<project-id>-tfstate --versioning

      cat > benchmarking/automation/infra/common/backend_override.tf <<'EOF'
      terraform {
        backend "gcs" {}
      }
      EOF

      terraform -chdir=benchmarking/automation/infra/common init \
        -backend-config=bucket=<project-id>-tfstate \
        -backend-config=prefix=<default_resource_name>
      ```

      `init` offers to copy an existing local state into the bucket.

4. Create everything, answering `yes` at the plan. A GKE cluster takes several
   minutes:

   ```bash
   terraform -chdir=benchmarking/automation/infra/common apply -var-file=terraform.tfvars
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

3. Build `kubectl-ate`

   The benchmark job scripts use it to delete the actors around a run. It is
   built from the submodule, and `go install` puts it in `$(go env GOPATH)/bin`,
   which has to be on `PATH`:

   ```bash
   (cd dependency-repos/substrate && go install ./cmd/kubectl-ate)
   ```

4. Deploying the benchmarking

   ```bash
   ./benchmarking/deploy-benchmarking.sh --deploy
   ```

5. Pick the test case to run

   A test case is a named entry in [`benchmarking/tests.yaml`](benchmarking/tests.yaml),
   under `cases:`. The key is what the next step passes to `--test-case`.

6. Run a benchmark job script.

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

7. Collect the artifacts

   When a job ends it uploads its files to `reports/<run-id>/<test-case>/` in
   the deployment's bucket. The run id is a UTC timestamp, e.g.
   `20260929t120951z`; `--run-id` sets it instead.

   Each job leaves:

   - `locust_stats.csv`, `locust_stats_history.csv`, `locust_failures.csv`,
     `locust_exceptions.csv` — locust's own output, written as the job goes.
     These are the run's latency percentiles: failed requests included, and
     rounded to 1 ms below 100 ms, 10 ms below 1 s, 100 ms below 10 s and 1 s
     above.
   - `custom_stats_history.csv`, `cluster_vitals_history.csv` — the job's rows
     read back out of Prometheus.
   - `run.json` - the case, how the run went, every image it ran by digest,
     and the commits they were built from. The locust image is also tagged
     with the run id in the registry.

   The scripts download them to `reports/<run-id>/<test-case>/`. See
   [Charting a run](#charting-a-run) to view them.

## Charting a run

[`benchmark-charts`](benchmark-charts) draws a downloaded run in a matplotlib
window: a request's latency percentiles and throughput, the worker nodes' CPU
and memory utilization, and the actor counts over time, with the run's
steady-state window shaded. It needs Python 3.9+.

1. Create a virtual environment and install the package into it:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install ./benchmark-charts
   ```

   In a new shell, activate it again before the next step.

2. Chart a run, from the repo root — runs are looked up in `./reports`:

   ```bash
   benchmark-report
   ```

   With no argument it charts the latest run. Pass an index to pick another,
   counted as a Python list is (`-2` the one before the latest, `0` the
   oldest), or a path:

   ```bash
   benchmark-report reports/<run-id>
   ```

   A job's folder, `reports/<run-id>/<test-case>`, charts that job alone.

   Drop-downs in the window switch between the run's jobs, a job's repeats,
   and the request the top panel charts. These pick what it opens on:

   - `--job <test-case>` — default: the first by name.
   - `--repeat <n>` — default: the first.
   - `--chart ttfi|execution-time|cycle-time` — default: `ttfi`.


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

Use terraform to destroy the cluster. **This also deletes the bucket and every
run's reports in it**, since the example sets `bucket_force_destroy = true`.
Download any worth keeping first, or set it to `false`, in which case destroy
stops at the bucket while it holds anything.

   ```bash
   terraform -chdir=benchmarking/automation/infra/common destroy
   ```
