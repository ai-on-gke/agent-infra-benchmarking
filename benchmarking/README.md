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
