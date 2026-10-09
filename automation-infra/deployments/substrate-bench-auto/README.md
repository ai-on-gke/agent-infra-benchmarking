# substrate-bench-auto

Runs every case in
[`scenarios.yaml`](../../../benchmarking/scenarios/scenarios.yaml) once a day,
at 3am UTC, in the GCP project `akvelon-gke-aieco` (`us-central1`). Each
execution runs the latest commit pushed to the `main` branch, and each
repeat of a case gets a cluster of its own, destroyed after it.

To run it now with one case rather than waiting for the schedule:

```bash
automation-infra/tools/trigger --deployment=substrate-bench-auto --scenario=glutton
```

How the deployment was created, and how to make another, is in the
[automation README](../../README.md).

## What it creates

Everything is named `substrate-bench-auto`, with a suffix where GCP needs
names to differ:

| What | Name |
|---|---|
| Cloud Run job | `substrate-bench-auto` |
| Cloud Scheduler job that starts it | `substrate-bench-auto` |
| Artifact Registry repository of the job's image | `substrate-bench-auto` |
| Service account the job, and so the scenario-runner, runs as | `substrate-bench-auto` |
| Service accounts of the scheduler, the image's build and GitHub Actions | `substrate-bench-auto-sched`, `-build`, `-gh` |
| Bucket Cloud Build stages the image's source in | `substrate-bench-auto-build-source` |

The bucket `substrate-bench-auto-tfstate` was created by hand, before the
deployment. It holds:

| Prefix | What |
|---|---|
| `automation/cloud-run/substrate-bench-auto/` | This deployment's Terraform state |
| `source/main/` | The commits the job runs, uploaded by the Upload source workflow on every push |
| `terraform/<run-id>/` | Each execution's scenario-runner state, for its runner infra and every repeat's cluster |

## Running it by hand

[`tools/trigger`](../../tools/trigger) starts an execution and prints its log
until it ends. Without arguments it runs every case, like the schedule. Any
argument goes to the scenario-runner, so `--scenario` picks the cases:

```bash
automation-infra/tools/trigger --deployment=substrate-bench-auto --scenario=glutton
```

To check the job works without creating a cluster, run the `dummy` case:

```bash
automation-infra/tools/trigger-dummy --deployment=substrate-bench-auto
```

Stopping the script leaves the execution running. To stop the execution
itself, cancel it:

```bash
gcloud run jobs executions cancel <EXECUTION> --project=akvelon-gke-aieco --region=us-central1
```

## Checking the scheduled runs

The GCP resources a scheduled execution creates are labeled
`benchmark-trigger=scheduled`, and those of one started by hand `manual`. To
list the recent executions and their status:

```bash
gcloud run jobs executions list --job=substrate-bench-auto --project=akvelon-gke-aieco --region=us-central1
```

To read the log of one of them:

```bash
gcloud logging read 'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="<EXECUTION>"' \
  --project=akvelon-gke-aieco --order=asc --format='value(textPayload)'
```

The scenario-runner copies the reports to `reports/` inside the container,
which goes when the execution ends, and the case's bucket is destroyed with its
cluster. So for now the log is all an execution leaves behind.

## Changing it

The job runs whatever is on `main`, so a change to the scenarios or
the scenario-runner only needs a push. Wait for the Upload source workflow to
finish before the next execution starts.

A change to the deployment itself, such as the schedule or the branch, goes in
[`main.tf`](main.tf), then is applied from the repo root:

```bash
terraform -chdir=automation-infra/deployments/substrate-bench-auto apply
```

Apply also rebuilds the job's image on Cloud Build when the repo's commit or
the image's files have changed.

To stop the scheduled runs for a while, pause the scheduler. The next apply
resumes it:

```bash
gcloud scheduler jobs pause substrate-bench-auto --project=akvelon-gke-aieco --location=us-central1
```

## Cleaning up after a failed execution

An execution destroys its clusters and runner infra even when a case fails. If
a destroy itself fails, the resources stay, with their state and config in
`gs://substrate-bench-auto-tfstate/terraform/<run-id>/`. Find them in the
console by the `benchmark-run-id=<run-id>` label, and see
[When a run fails or is stopped](../../../benchmarking/README.md#when-a-run-fails-or-is-stopped)
for destroying them.

## Removing it

Destroying the deployment deletes everything in [What it creates](#what-it-creates),
but not the state bucket, the APIs it enabled, or the repo secret
`GCP_CREDENTIALS_SUBSTRATE_BENCH_AUTO`:

```bash
terraform -chdir=automation-infra/deployments/substrate-bench-auto destroy
```

Then take `substrate-bench-auto` out of the `deployment` matrix of both jobs in
[`upload-source.yml`](../../../.github/workflows/upload-source.yml), or every
push fails to upload.
