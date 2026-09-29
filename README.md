# agent-infra-benchmarking
Backend for benchmarks and corresponding recipes for agent infra offerings.

## Setting up the infrastructure

The cluster, the snapshot bucket and the Artifact Registry repository come from
the Terraform config in `infra/terraform-gke`. Run from the repo root, with
gcloud application-default credentials already in place.

1. Copy the example — the real file is gitignored and per-developer:

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

2. Download the providers:

   ```bash
   terraform -chdir=infra/terraform-gke init
   ```

3. Create everything, answering `yes` at the plan. A GKE cluster takes several
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

## Benchmarking agent-sandbox (opt-in backend)

The steps above drive **substrate**, the default backend. The same harness can
instead drive [`sigs.k8s.io/agent-sandbox`](https://github.com/kubernetes-sigs/agent-sandbox),
selected with `--backend agent-sandbox`. It measures the wall clock a client
waits before a freshly provisioned sandbox accepts work, across two
provisioning paths:

- **ColdCreate** — a `Sandbox` created directly from a `SandboxTemplate`'s
  blueprint, through to `Ready` and a first request served. A true cold boot:
  image pull, scheduling and application start are all inside the window.
- **PoolClaim** — a `SandboxClaim` adopting an already-running pod from a
  `SandboxWarmPool`. Cheaper provisioning — a label flip onto a warm pod —
  and the "warm" number ColdCreate is compared against.

The workload run inside the sandbox is a minimal JSON-echo probe
([`benchmarking/workloads/probe`](benchmarking/workloads/probe)), addressed at
each Sandbox's own per-pod Service (agent-sandbox needs no atenet-style shared
router). It is the agent-sandbox analog of substrate's `glutton`.

### How it maps to substrate, and what does not carry over

| substrate | agent-sandbox | notes |
| --- | --- | --- |
| ActorTemplate | SandboxTemplate | blueprint a workload is provisioned from |
| WorkerPool (bin-packed actors per pod) | SandboxWarmPool (one Sandbox+Pod per replica) | density differs — see below |
| CreateActor + FirstResume | ColdCreate | first activation |
| WarmStart (live-memory snapshot restore) | PoolClaim | **not** the same latency class — see below |
| atenet router + `ate-target-actor` header | per-pod Service DNS (`status.serviceFQDN`) | direct addressing |
| ateapi mTLS pod identity | `benchmark-runner` ServiceAccount + RBAC | how the worker reaches the control plane |

**These are different systems; their numbers are not directly comparable.**
Two reasons in particular:

- **PoolClaim is not a live-memory restore.** Substrate's WarmStart resumes an
  actor's own prior in-memory snapshot (its ~100 ms activation target). A pool
  claim adopts a pod that booted from its image and never held this workload's
  state. A live-memory warm-start on agent-sandbox would need GKE Pod
  Snapshots and is **not implemented here** — it is deliberately out of scope,
  not conflated with WarmStart.
- **Density differs.** A substrate WorkerPool hosts many actor sandboxes per
  worker pod; a SandboxWarmPool replica is its own Sandbox and Pod.

### Running it

The boomer-worker binary supports `--backend agent-sandbox` end to end, and
`workloads/deploy.sh` / `build_and_push.sh` stand up its workload infra with
plain `kubectl` — no substrate submodule, `BUCKET_NAME`, `ko`, or `kubectl-ate`
required. `PROBE_IMAGE` names the probe image (the agent-sandbox counterpart of
`OPENCLAW_IMAGE`); export it or set it in `.env.sh`.

```bash
# 1. Build and push the locust image (boomer-worker baked in) and the probe.
PROBE_IMAGE=<registry>/ping-probe:latest \
  ./benchmarking/locust/build_and_push.sh --backend agent-sandbox

# 2. Deploy the ping SandboxTemplate + SandboxWarmPool and the benchmark-runner
#    RBAC into the `benchmarking` namespace. --worker-count sizes the warm pool;
#    --runtime-class gvisor on GKE Sandbox (omit on kind for the default runtime).
PROBE_IMAGE=<registry>/ping-probe:latest \
  ./benchmarking/workloads/deploy.sh --backend agent-sandbox --deploy \
    --worker-count 5 --runtime-class gvisor
```

The boomer-worker is then driven with `--backend=agent-sandbox`
`--namespace=benchmarking` `--workload-name=ping` (see
`cmd/benchmarking/boomer-worker --help` and
[`internal/benchmarking/boomer/runworkload`](internal/benchmarking/boomer/runworkload)).

> **Note:** the `run_locust_job.sh` job runner and its report generation
> (`ttfi-vs-users.html`, keyed on substrate's TTFI series) still assume the
> substrate WorkerPool and the ateapi mTLS job manifest, so they do not yet
> drive `--backend agent-sandbox` unattended. Threading the backend through the
> job runner, the `tests.yaml` cases, and a Prometheus report for the
> ColdCreate/PoolClaim series is the remaining follow-up; the pieces above make
> the backend runnable and its infra deployable today.

Tear the workload infra down with:

```bash
./benchmarking/workloads/deploy.sh --backend agent-sandbox --delete
```

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
