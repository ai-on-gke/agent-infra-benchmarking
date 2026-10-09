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
- gcloud's `gke-gcloud-auth-plugin` (`gcloud components install
  gke-gcloud-auth-plugin`), which kubectl reaches the clusters through
- Docker with Buildx, unless you pass `--cloud-build`: the image builds use
  `--build-context`
- curl and a `tar` with zstd support, for `sandbox-class: microvm` cases

### Cost and quota

Each run of a case applies a cluster of its own in one zone (`us-central1-a`)
and destroys it afterwards. It has a `default` node, `c2-standard-16`
(`default_machine_type`); the `sandboxed` workers, `sandboxed_node_count` of
`sandboxed_machine_type` (one `c2-standard-16` in the shipped cases); and an
`e2-standard-4` for Prometheus. The openclaw case adds an `e2-standard-8` for
its mock LLM. So a run needs at least 32 vCPUs of C2 quota in the region, all
billed while it is up. Check it with:

```bash
gcloud compute regions describe us-central1 --project <project-id> \
  --flatten=quotas --format="table(quotas.metric,quotas.usage,quotas.limit)" |
  grep -E "^(METRIC|C2_CPUS) "
```

Short of C2, set `default_machine_type` and `sandboxed_machine_type` in the
case's `tfvars_inline` to another family, such as `n2-standard-16` (which the
`microvm` class also runs on). To deploy into a VPC you already have rather
than a new one per run, set `existing_network`; see its description in
[`variables.tf`](scenarios/infra/clusters/common/variables.tf).

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

## Running scenarios

```bash
./benchmarking/tools/run --scenario glutton
```

To run more than one case, repeat `--scenario`. Without it, every case in
`scenarios.yaml` runs, in the order listed. Each repeat of a case gets a fresh
cluster. Reports are copied to `reports/<run-id>/<case>/` at the repo root.

Useful flags (`--help` lists them all):

| Flag | |
|---|---|
| `--scenario NAME` | case to run; repeatable |
| `--name RUN_ID` | run id, default `run-<UTC timestamp>` |
| `--cloud-build` | build images on Cloud Build rather than local Docker |
| `--terraform-output json` | log Terraform as JSON |

`tools/run-local` is `tools/run --state-backend local`.

A run fails when more than its case's `max-failure-ratio` of requests fail
(0.01 by default), or when it makes no requests. A failed case does not stop
the ones after it; the execution exits non-zero at the end, naming them. A run
that outlives the hour the scenario-runner waits for it does stop the
execution, as it is still going.

Ctrl-C stops the run in progress the way its end would: the master stops the
worker, which deletes its actors, and uploads what it recorded, and the
reports are downloaded before the cluster is destroyed. Terraform is left to
finish what it has in flight rather than killed. A second Ctrl-C forces it.

Substrate blocks all outbound traffic from an actor that has no egress policy.
The worker gives each actor one that allows only the hosts named by URLs in
its ActorTemplate's env, such as the OTLP collector and openclaw's mock LLM. To
let actors reach another service, add its URL to the template, or set the
case's `worker: actor-egress-allow` to a comma-separated list of hosts.

### What a run leaves

Each run of a case uploads its files to `reports/<run-id>/<record>/` in its
cluster's bucket, `<record>` being the case's name, or `<case>-<n>` for each of
several repeats, and the scenario-runner copies them to the same path under
`reports/` at the repo root. The bucket goes with the cluster, so the local copy
is the one that lasts; on Cloud Run there is none. Each folder holds:

- `locust_stats_steady.csv`: the run's latency percentiles over the
  steady-state window; see [Reading the results](#reading-the-results).
- `locust_stats.csv`, `locust_stats_history.csv`, `locust_failures.csv`,
  `locust_exceptions.csv`: locust's own output, over the whole run.
- `custom_stats_history.csv`, `cluster_vitals_history.csv`: actor counts and
  node CPU and memory over the run, read back out of Prometheus.
- `locust_steady_window.json`: the steady-state window's bounds.
- `run.json`: how the run went, the resolved case, the cluster and its nodes,
  the WorkerPool, ActorTemplate and SandboxConfig, every image by digest, and
  the commits. `README.md` is the same for a reader.
- `cleanup.json`: the actors the scenario-runner found left after the run and
  deleted, and any still there a minute later, such as one stuck in
  `ACTOR_STATE_DELETING` (agent-substrate/substrate#2011).

A case with several repeats also gets `<case>-summary.csv` and
`<case>-summary.md` in `reports/<run-id>/`: the min, median and max of every
steady-state stat over the repeats that passed.

### Charting a run

[`benchmark-charts`](../benchmark-charts) draws a downloaded run in a
matplotlib window: a request's latency percentiles and throughput, the worker
nodes' CPU and memory, and the actor counts over time, with the steady-state
window shaded. It needs Python 3.9+:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install ./benchmark-charts
benchmark-report reports/<run-id>
```

With no argument it charts the latest run in `./reports`.

### Dev infra

To iterate without creating a cluster on every run, use `--dev-infra NAME`. It
keeps the runner infra and cluster between executions, and repeats run one
after another on that cluster. Each scenario needs its own `--dev-infra` name,
because a dev infra keeps the cluster of the case it was first created for:

```bash
./benchmarking/tools/run --dev-infra glutton-dev --scenario glutton
# reuse the images and workloads already deployed
./benchmarking/tools/run --dev-infra glutton-dev --scenario glutton --no-build --no-deploy-workloads

./benchmarking/tools/dev-infra list
./benchmarking/tools/dev-infra destroy glutton-dev
```

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
own cluster unless `--dev-infra` is given. Compare cases across their repeats,
not by a single one: `<case>-summary.csv` gives the min, median and max of
each stat over them, as the example's `glutton-summary.csv` does.
