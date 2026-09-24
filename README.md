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

2. Download the providers:

   ```bash
   terraform -chdir=infra/terraform-gke init
   ```

3. Create everything, answering `yes` at the plan. A GKE cluster takes several
   minutes, and this is billable:

   ```bash
   terraform -chdir=infra/terraform-gke apply
   ```

## Setting up the Agent Substrate


1. Configure the environment

   Copy the example and fill in the values for your project:

   ```bash
   cp env.sh.example .env.sh
   ```

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


4. Run a benchmark job script.

   ```bash
   ./benchmarking/run_locust_job.sh --test-case glutton-baseline
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

TBD, and only if the cluster is no longer wanted — it is created outside this
repo, so it is deleted outside it too.
