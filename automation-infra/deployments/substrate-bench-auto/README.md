# substrate-bench-auto

Runs the cases of
[`scenarios.yaml`](../../../benchmarking/scenarios/scenarios.yaml) that `args`
in [`main.tf`](main.tf) names, the gVisor and microvm pairs, once a day,
at 3am UTC, in the GCP project `akvelon-gke-aieco` (`us-central1`). Each
execution runs the latest commit pushed to the `main` branch, and each
repeat of a case gets a cluster of its own, destroyed after it.

To run it now with one case rather than waiting for the schedule:

```bash
automation-infra/tools/trigger --deployment=substrate-bench-auto --scenario=glutton
```

Every run's reports can be queried in BigQuery, in the dataset
[`substrate_bench_auto`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&page=dataset),
as in [Querying the reports](#querying-the-reports):

```bash
bq query --project_id=akvelon-gke-aieco --use_legacy_sql=false \
  'SELECT run_id, test_case, requests_per_sec, p50_ms, p95_ms FROM substrate_bench_auto.locust_stats_steady WHERE name = "CycleTime" ORDER BY 1, 2'
```

How the deployment was created, and how to make another, is in the
[automation README](../../README.md).

## What it creates

Everything is named `substrate-bench-auto`, with a suffix where GCP needs
names to differ:

| What | Name |
|---|---|
| Cloud Run job | `substrate-bench-auto` |
| Cloud Scheduler job that starts it, daily at 03:00 UTC | `substrate-bench-auto` |
| Cloud Run job, and its scheduler, that destroys what a killed execution left (`tools/destroy-leftovers`), daily at 15:00 UTC | `substrate-bench-auto-cleanup` |
| Alert policy on a failed execution of either job | `substrate-bench-auto: failed execution` |
| Artifact Registry repository of the job's image and the images its runs pull, which the Publish workflow pushes to | `substrate-bench-auto` |
| Service account the job, and so the scenario-runner, runs as | `substrate-bench-auto` |
| Service accounts of the scheduler, the runs' cluster nodes and GitHub Actions | `substrate-bench-auto-sched`, `-nodes`, `-gh` |
| Workload Identity pool and provider GitHub Actions authenticate through | `substrate-bench-auto-gh`, `github` |
| Network, subnetwork and Cloud NAT the runs' clusters go into, with an internal control-plane endpoint only, and the job joins | `substrate-bench-auto` |
| BigQuery dataset of the reports' views | [`substrate_bench_auto`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&page=dataset) |

The bucket `substrate-bench-auto-tfstate` was created by hand, before the
deployment. It holds:

| Prefix | What |
|---|---|
| `automation/cloud-run/substrate-bench-auto/` | This deployment's Terraform state |
| `terraform/<run-id>/` | Each execution's scenario-runner state, for its runner infra and every repeat's cluster |
| `data/<run-id>/<record>/` | Each repeat's reports: locust's CSVs, the custom and cluster-vitals histories, and the Jobs' logs |

## Running it by hand

[`tools/trigger`](../../tools/trigger) starts an execution and prints its log
until it ends. Without arguments it runs the cases `args` in [`main.tf`](main.tf)
names, like the schedule. Any argument goes to the scenario-runner instead, so
`--scenario` picks the cases:

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

## Querying the reports

Each repeat's reports stay in `gs://substrate-bench-auto-tfstate/data/<run-id>/<record>/`,
`<record>` being the case's name, or `<case>-<n>` for each of its repeats. The
BigQuery dataset `substrate_bench_auto` reads them where they are, so a run's
reports can be queried as soon as it has written them, with nothing to load.

### Where the data is

In the console, open [BigQuery Studio](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco)
and, in the Explorer, `akvelon-gke-aieco` → `substrate_bench_auto`, or go
straight to
[the dataset](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&page=dataset).
Each view's name below links to it. Query the views, one per report file
across every run:

| View | Rows | From |
|---|---|---|
| [`locust_stats_history`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&t=locust_stats_history&page=table) | One a second per request name, and `Aggregated` | `locust_stats_history.csv` |
| [`locust_stats`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&t=locust_stats&page=table) | One per request name, over the whole run | `locust_stats.csv` |
| [`locust_stats_steady`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&t=locust_stats_steady&page=table) | One per request name, over the steady-state window | `locust_stats_steady.csv` |
| [`custom_stats_history`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&t=custom_stats_history&page=table) | One a second per worker gauge: `ActiveActors`, `IdleActors` | `custom_stats_history.csv` |
| [`cluster_vitals_history`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&t=cluster_vitals_history&page=table) | One a second per node metric, `type` being the node | `cluster_vitals_history.csv` |
| [`locust_steady_window`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&t=locust_steady_window&page=table) | One per repeat: its steady-state window's `start` and `end` | `locust_steady_window.json` |

The `*_files` tables beside them are the files as written, every column a
string, which the views cast.

Every view has these columns to filter or group by:

| Column | What |
|---|---|
| `run_id` | The execution, e.g. `run-20261009t191551z` |
| `test_case` | The record, e.g. `glutton-2` |
| `scenario` | The case in `scenarios.yaml`, e.g. `glutton`, so a case's repeats can be compared |

The per-second views also have `time`, `elapsed_secs` since the repeat's first
row, which lines repeats up against each other, and `steady`, whether the row
is in the steady-state window. A run without a `locust_steady_window.json`
has `steady` null. Response times are in milliseconds, e.g. `p50_ms`, and a
value locust wrote as `N/A` is null.

### Example queries

To list the runs there are, with how long each repeat ran and how much of it
was steady:

```bash
bq query --project_id=akvelon-gke-aieco --use_legacy_sql=false \
  'SELECT run_id, test_case, COUNT(*) AS secs, COUNTIF(steady) AS steady_secs
   FROM substrate_bench_auto.locust_stats_history WHERE name = "Aggregated" GROUP BY 1, 2 ORDER BY 1, 2'
```

To compare a case's repeats over their steady-state windows:

```bash
bq query --project_id=akvelon-gke-aieco --use_legacy_sql=false \
  'SELECT run_id, test_case, request_count, failure_count, requests_per_sec, p50_ms, p95_ms, p99_ms
   FROM substrate_bench_auto.locust_stats_steady WHERE scenario = "glutton" AND name = "CycleTime" ORDER BY 1, 2'
```

To follow the worker's active actors through each repeat, second by second:

```bash
bq query --project_id=akvelon-gke-aieco --use_legacy_sql=false \
  'SELECT run_id, test_case, elapsed_secs, value AS active_actors
   FROM substrate_bench_auto.custom_stats_history WHERE name = "ActiveActors" ORDER BY 1, 2, 3'
```

### Charting the panels of benchmark-charts

Three more views have what each panel of
[benchmark-charts](../../../benchmark-charts) draws, a row a second, with the
same `run_id`, `test_case` and `scenario`, and `steady`:

| View | Panel | Columns to chart |
|---|---|---|
| [`latency_chart`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&t=latency_chart&page=table) | A request's latency and throughput; `request` picks it, e.g. `TTFI`, `ExecutionTime`, `CycleTime` | `p50_ms` to `p100_ms`, `requests_per_sec`, `successful_per_sec`, `failures_per_sec` |
| [`pool_chart`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&t=pool_chart&page=table) | Each worker-pool node's utilization; `node` is the last two parts of its name | `cpu_percent`, `memory_percent` |
| [`actors_chart`](https://console.cloud.google.com/bigquery?project=akvelon-gke-aieco&p=akvelon-gke-aieco&d=substrate_bench_auto&t=actors_chart&page=table) | All actors, which is locust's user count, and the worker's active and idle ones | `actors`, `active_actors`, `idle_actors` |

In all three, `elapsed_secs` counts from the repeat's locust start, so their
charts line up. A latency is null for a second with no requests, as locust
writes 0 for it, so the line breaks there rather than dropping to zero.

To chart one, run a query in BigQuery Studio, open the **Chart** tab under the
results, and pick a line chart with `elapsed_secs` as the dimension and the
columns as measures. The **Query** button on a view leaves the column list
empty, so put the columns in, as in these. Change `run_id` and `test_case` to
chart another repeat.

A request's latency and throughput, with `request` set to the one to chart:

```sql
SELECT elapsed_secs, p50_ms, p95_ms, p99_ms, requests_per_sec, successful_per_sec, failures_per_sec
FROM substrate_bench_auto.latency_chart
WHERE run_id = 'run-20261009t191551z' AND test_case = 'glutton-1' AND request = 'TTFI'
ORDER BY elapsed_secs
```

The worker pool's CPU, a line per node with `node` as the chart's series:

```sql
SELECT elapsed_secs, node, cpu_percent, memory_percent
FROM substrate_bench_auto.pool_chart
WHERE run_id = 'run-20261009t191551z' AND test_case = 'glutton-1'
ORDER BY elapsed_secs
```

The actors:

```sql
SELECT elapsed_secs, actors, active_actors, idle_actors
FROM substrate_bench_auto.actors_chart
WHERE run_id = 'run-20261009t191551z' AND test_case = 'glutton-1'
ORDER BY elapsed_secs
```

A case's repeats against each other, a line per repeat with `test_case` as the
series:

```sql
SELECT elapsed_secs, test_case, p50_ms
FROM substrate_bench_auto.latency_chart
WHERE scenario = 'glutton' AND request = 'TTFI'
ORDER BY elapsed_secs
```

The steady-state window isn't shaded as in benchmark-charts; add `AND steady`
to chart only that part.

### Charting and switching between tests

For a quick chart of a query, run it in BigQuery Studio and open the **Chart**
tab under the results. For a dashboard to switch between tests in, choose
**Open in** → **Looker Studio** on the results, then add a drop-down list
control on `run_id`, `test_case` or `scenario`. Every chart on the page follows
it. To overlay repeats, put `elapsed_secs` on the x-axis and `test_case` as the
breakdown dimension.

### Access

The views read the files with the querying user's own credentials. Besides
seeing the dataset, that takes `roles/bigquery.jobUser` on the project and
read access to the files in `gs://substrate-bench-auto-tfstate/data/`. The
project's owners and editors have both.

### Adding reports from elsewhere

A run's reports from somewhere else, such as a local run's `reports/` folder,
can be added by copying them in under the same layout. They show up in the
views on the next query:

```bash
gcloud storage cp -r --no-clobber reports/<run-id> gs://substrate-bench-auto-tfstate/data/
```

The views take the CSVs' columns by position, as locust 2.32 writes them
with its default percentiles. A view errors until at least one run has
written its file.

## Changing it

The job runs whatever is on `main`, so a change to the scenarios, the
scenario-runner or the job's image only needs a push. Wait for the Publish
workflow to finish before the next execution starts.

A change to the deployment itself, such as the schedule, the branch, the cases
the nightly runs (`args`) or who is told of a failed execution
(`alert_emails`), goes in [`main.tf`](main.tf), then is applied from the repo
root:

```bash
terraform -chdir=automation-infra/deployments/substrate-bench-auto apply
```

To stop the scheduled runs for a while, pause the scheduler. The next apply
resumes it:

```bash
gcloud scheduler jobs pause substrate-bench-auto --project=akvelon-gke-aieco --location=us-central1
```

## Cleaning up after a failed execution

An execution destroys its clusters and runner infra even when a case fails. If
a destroy itself fails, or the execution is killed, the resources stay, with
their state and config in `gs://substrate-bench-auto-tfstate/terraform/<run-id>/`.
The cleanup job destroys them at 15:00 UTC once their execution has ended. To
do it sooner, run the same tool by hand:

```bash
automation-infra/tools/destroy-leftovers --deployment=substrate-bench-auto
```

## Removing it

Destroying the deployment deletes everything in [What it creates](#what-it-creates)
and the repo variables `GCP_WORKLOAD_IDENTITY_PROVIDER_SUBSTRATE_BENCH_AUTO` and
`GCP_SERVICE_ACCOUNT_SUBSTRATE_BENCH_AUTO`, but not the state bucket or the APIs
it enabled. The Workload Identity pool stays
deleted for 30 days, during which its name cannot be used again:

```bash
terraform -chdir=automation-infra/deployments/substrate-bench-auto destroy
```

The destroy stops at the subnetwork, as "already being used by ...
serverless-ipv4-cloudrun-...": Cloud Run keeps the job's addresses in it for up
to a couple of hours after the job is deleted. Run it again once they are gone.

Then take `substrate-bench-auto` out of the `deployment` matrix of every job in
[`publish.yml`](../../../.github/workflows/publish.yml), or every
push fails to push.
