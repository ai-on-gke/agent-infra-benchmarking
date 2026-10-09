# Benchmarking

The scenario-runner runs the benchmark cases in
[scenarios/scenarios.yaml](scenarios/scenarios.yaml). For each run of a case it
applies a GKE cluster with Terraform, installs substrate on it, runs locust
against it and destroys it afterwards.

## Prerequisites

- A GCP project where you can create GKE clusters, Artifact Registry
  repositories and GCS buckets
- Python 3.11
- Terraform
- gcloud
- Go, kubectl and git
- Docker, unless you pass `--cloud-build`
- curl and a `tar` with zstd support, for `sandbox-class: microvm` cases

## Setup

1. Check out the substrate submodule:

   ```bash
   git submodule update --init
   ```

2. Log in to gcloud:

   ```bash
   gcloud auth login
   ```

3. Log in for Application Default Credentials, which the Python code and
   Terraform use:

   ```bash
   gcloud auth application-default login
   ```

4. Create `benchmarking/.env.sh` and set `PROJECT_ID` and the state backend in it:

   ```bash
   cp benchmarking/.env.sh.example benchmarking/.env.sh
   ```

   Each value can also be passed as a flag instead (`--project-id`,
   `--state-backend`, `--state-bucket`, `--state-prefix`).

The Python venv (`tools/venv_run`) is created the first time you run a
`tools/` script, and recreated whenever `scenario_runner/requirements.txt`
changes.

To check the setup, run the `dummy` case. It applies and destroys its Terraform
without creating a cluster:

```bash
./benchmarking/tools/run-local-dummy
```

## Running scenarios from your machine

```bash
./benchmarking/tools/run --scenario glutton
```

To run more than one case, repeat `--scenario`; they run in the order given.
Without it, every case in `scenarios.yaml` runs, in the order listed. A case
with `repeats: N` runs N times, recorded as `<case>-1` to `<case>-N`, each on a
fresh cluster. A failed repeat doesn't stop the ones after it.

Useful flags (`--help` lists them all):

| Flag | |
|---|---|
| `--scenario NAME` | case to run; repeatable |
| `--name RUN_ID` | run id, default `run-<UTC timestamp>`. It starts the names of the GCP resources, so keep it lowercase |
| `--project-id ID` | overrides `PROJECT_ID` |
| `--region REGION` | default `us-central1` |
| `--state-backend local\|gcs` | overrides `STATE_BACKEND`, see [Terraform state](#terraform-state) |
| `--cloud-build` | build images on Cloud Build rather than local Docker |
| `--no-docker-login` | push with Docker's own registry credentials rather than gcloud's access token |
| `--scenarios-file PATH` | another scenarios file, e.g. a local copy with a smaller load |
| `--terraform-output json` | log Terraform as JSON |

`tools/run-local` is `tools/run --state-backend local`.

### What a run does

1. Applies `scenario_runner/infra`: an Artifact Registry repository for the
   run.
2. For each repeat of each case:
   1. Applies the case's cluster from `scenarios/infra/clusters/<name>/`: a
      GKE cluster, its node pools, a GCS bucket for the reports and the IAM
      the benchmark needs.
   2. Builds substrate from the submodule with ko and installs it, unless the
      case sets `skip-substrate-install`.
   3. Builds and pushes the locust and workload images
      (`locust/images.json`, `workloads/images.json`).
   4. Deploys Prometheus, the worker pool and the case's workload, then the
      locust master and boomer worker Jobs, and waits for both to end.
   5. Uploads both Jobs' logs to the bucket beside the master's CSVs, and
      copies everything to `reports/<run-id>/<record>/`.
   6. Destroys the cluster, and its bucket with it.
3. Destroys `scenario_runner/infra`.

Most of a repeat's time is the cluster's apply and destroy. The `common`
cluster uses `c2-standard-16` nodes for the sandboxed
pool, so check the project's C2 CPU quota in the region before raising
`sandboxed_node_count`.

### Where things end up

| What | Where |
|---|---|
| Reports and Job logs | `reports/<run-id>/<record>/` at the repo root. The bucket is destroyed with the cluster, so this is the only copy left |
| Terraform deployments | `scenario_runner/.local-data/runs/terraform/<run-id>/`: `scenario-runner/` and `scenarios/<record>/`, each with `config/`, `tf-data/` and, with the local backend, `tfstates/` |
| The cluster's kubeconfig | `tf-data/kubeconfig` in the repeat's deployment |
| GCP resources | labeled `benchmark-run-id=<run-id>` and `benchmark-test-case=<record>` |

Both `.local-data/` and `reports/` are gitignored; a report worth committing,
like the example below, needs an exception in `.gitignore`.

To look at the cluster while a repeat runs:

```bash
export KUBECONFIG=benchmarking/scenario_runner/.local-data/runs/terraform/<run-id>/scenarios/<record>/tf-data/kubeconfig
kubectl -n benchmarking get pods
kubectl -n benchmarking port-forward deploy/prometheus 9090
```

### Images and versions

The images are tagged with the repo's `HEAD` commit, but built from the
working tree as it is, uncommitted changes included. Commit before a run whose
results you want to tie to a commit. The images are built for `linux/amd64`,
the nodes' platform, whatever the build host's; on an ARM machine that needs
Docker's emulation, or `--cloud-build`.

Substrate is built from `dependency-repos/substrate` as checked out, and its
version is `git describe --tags --always --dirty` there, or `VERSION` if set.
The cluster's nodes are labeled with it.

### Terraform state

With `local`, each deployment's state is a file under
`.local-data/runs/terraform/<run-id>/`, so only this machine can destroy what
a run left behind. With `gcs`, it is in `gs://<state bucket>/<state prefix>/<run-id>/`
together with a copy of the rest of the deployment, so a destroy needs nothing
but the bucket. Use `gcs` for long runs, or when several people share the
project.

### When a run fails or is stopped

A failed apply, install or benchmark still destroys the repeat's cluster, and
the run destroys `scenario_runner/infra` before it exits. So does Ctrl-C;
press it once and let the destroy finish.

If a destroy itself fails, the resources stay. Run it again by hand from the
deployment's `config/`:

```bash
cd benchmarking/scenario_runner/.local-data/runs/terraform/<run-id>/scenarios/<record>/config
TF_DATA_DIR=../tf-data terraform destroy
```

Destroy the repeats' clusters before `scenario-runner/`. Anything still left
can be found in the console by the `benchmark-run-id` label.

### Dev infra

To iterate without creating a cluster on every run, use `--dev-infra NAME`. It
keeps the runner infra and cluster between executions, and repeats run one
after another on that cluster. Each scenario needs its own `--dev-infra` name,
because a dev infra keeps the cluster of the case it was first created for:

```bash
./benchmarking/tools/run --dev-infra glutton-dev --scenario glutton
# reuse the images and workloads already deployed
./benchmarking/tools/run --dev-infra glutton-dev --scenario glutton --no-build --no-deploy-workloads
# reapply the cluster config after changing it, reinstalling substrate if its version changed
./benchmarking/tools/run --dev-infra glutton-dev --scenario glutton --dev-infra-update

./benchmarking/tools/dev-infra list
./benchmarking/tools/dev-infra outputs glutton-dev
./benchmarking/tools/dev-infra destroy glutton-dev
```

A dev infra's state is always local, in
`scenario_runner/.local-data/dev-infra/terraform/<name>/`, and its kubeconfig
in `cluster/tf-data/kubeconfig` there. It keeps costing money until it is
destroyed. `--no-build` needs the images of the current `HEAD` already pushed
to it.

### Adding a case

Add an entry under `cases:` in `scenarios.yaml`. The comments at the top of that
file describe each key. To change the load or the worker pool, add a new case
rather than editing an existing one, so results stay tied to the case name
they were recorded under.

## Reading the results

The committed example is run
[`reports/20261001t184243z/`](../reports/20261001t184243z/): the `glutton` case
at 100 users on 100 workers and 30 `c2-standard-16` nodes, three repeats,
recorded before the scenario-runner. Its
[`glutton-summary.md`](../reports/20261001t184243z/glutton-summary.md) has the
numbers, the hardware and the versions; each repeat's folder has its own
`README.md`. Steady-state TTFI there is p50 230 ms (230-240 across the
repeats) and p99 1100 ms (920-1300), at about 93 requests/s.

### What each row measures

The worker takes each actor through resume, one instruction and suspend, and
times the pieces. The request names in the CSVs are:

| Name | What it times |
| --- | --- |
| `TTFI` | Time to first instruction: from the start of the `ResumeActor` (explicit mode) or of the instruction that wakes the actor (implicit mode) until that instruction has been served and its answer checked. It includes the instruction's own service time. |
| `ExecutionTime` | The instruction's round trip to the actor on its own. |
| `CycleTime` | The whole cycle: `TTFI` and then the suspend. |
| `Resume` / `Suspend` | The `ResumeActor` / `SuspendActor` calls on their own. |
| `CreateActor` / `CreateActorEgressPolicy` / `CreateAtespace` / `DeleteActor` | The lifecycle calls. `CreateAtespace` is once per run; `DeleteActor` is mostly the teardown. |
| `ActiveActors` / `IdleActors` | Gauges in `custom_stats_history.csv`: actors resumed and actors suspended. |

Every call has a deadline, the case's `workload-timeout`; a call past it
counts as failed.

### Which file is authoritative

`locust_stats_steady.csv` is the number to quote for a repeat. It comes from
locust, which counts failed requests too and keeps response times at 1 ms
resolution below 100 ms, 10 ms below 1 s, 100 ms below 10 s and 1 s above.

`custom_stats_history.csv` is read back out of Prometheus. The worker's
histograms there use power-of-two buckets (1, 2, 4, ... 32768 ms) and count
successes only, so they show a curve's shape, not an exact tail. Where it and
locust disagree, locust wins.

### Steady state, and what the p99 is made of

- **The ramp is excluded.** `locust_stats.csv` covers the whole run, the
  ramp included. `locust_stats_steady.csv` starts the case's `warm-up` after
  the last user has started; a case without `warm-up` writes none.
- **The p99 is a few stalls, not a slow cluster.** In every repeat of the
  example, the steady-state window holds one or two stalls of 6-9 s in which
  TTFI throughput about halves and p50 briefly rises to 660-800 ms
  (`glutton-1` at 18:45:03 and 18:46:28 UTC, `glutton-2` at 18:49:59,
  `glutton-3` at 18:55:23). The rest of the time p50 sits at 230-250 ms. The
  ~1 s p99 is those stalls. Their cause is not established yet, and the
  cluster vitals cannot show it: they hold CPU and memory only. Quote p50, and
  name the tail, rather than quote a bare p99.

### Repeats

One run is one sample. A case's `repeats` runs it that many times, each on its
own cluster unless `--dev-infra` is given. Compare cases across their repeats, not by a single one; the
example's `glutton-summary.csv` gives the min, median and max of each stat
over its repeats.
