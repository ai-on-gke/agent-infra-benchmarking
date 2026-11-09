# agent-infra-benchmarking

Reproducible benchmarks of agent infrastructure on GKE: how long an agent's
sandbox takes to wake, serve an instruction and go back to sleep, under load,
on each platform and sandbox class. The first offerings measured are
[substrate](https://github.com/agent-substrate/substrate) on its own gVisor
and microvm sandboxes, and
[agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) on GKE
Sandbox's gVisor. A side case compares substrate's suspend and resume with
E2B, Modal, Daytona, Anthropic Managed Agents and OpenAI's hosted
environments, and prices them.

Every number comes from a named case in
[`benchmarking/scenarios/scenarios.yaml`](benchmarking/scenarios/scenarios.yaml),
run by the scenario-runner on a cluster it creates for the run and destroys
afterwards, so a result is reproduced by running its case again. A scheduled
job runs the main cases every 6 hours and keeps their reports in BigQuery.

## What is measured

A load generator (locust, with a Go boomer worker) drives N virtual users.
Each user owns one actor and takes it round a cycle: resume it, send it one
instruction, check the answer, suspend it. The worker times each step; the
names below are the rows of a run's CSVs and BigQuery views.

| Row | What it times |
|---|---|
| `TTFI` | Time to first instruction: from the start of the resume until the instruction's answer has been checked |
| `ExecutionTime` | The instruction's round trip on its own |
| `Resume`, `Suspend` | The control-plane calls on their own, which is where sandbox classes differ |
| `CycleTime` | The whole cycle |
| `CreateActor`, `DeleteActor`, … | The lifecycle calls |

Each run also records node CPU and memory, the count of active and idle
actors, and provenance: the commit, every image's digest, the cluster's
machine types and the installed versions. Quote `locust_stats_steady.csv`,
which leaves out the ramp-up, and the p50; give the p99 with its range over
repeats. [Reading the results](benchmarking/README.md#reading-the-results)
explains each file.

### Workloads

| Workload | What the actor runs | What a cycle costs |
|---|---|---|
| `glutton` | substrate's own echo server; the instruction is one HTTP ping with a nonce. Variants write RAM and disk before the suspend to size the snapshot. | Milliseconds, so it measures the platform |
| `openclaw` | The [openclaw](https://www.npmjs.com/package/openclaw) agent running one tool-calling turn against a deterministic mock LLM in the cluster | A whole agent turn, tens of seconds, so compare its `Resume` and `Suspend` rather than `TTFI` |

### Platforms

| Install | Sandbox class | How |
|---|---|---|
| `substrate` | `gvisor` | substrate's own runsc workers |
| `substrate` | `microvm` | Cloud Hypervisor guests on nodes with nested virtualization, or on bare metal |
| `agent-sandbox` | `gvisor` | Pods in GKE Sandbox; each cycle claims a fresh pod from a warm pool |

## The cases

Cases that differ only in their platform share one template, so they compare.
The main ones:

| Case | Load | Nodes | Purpose |
|---|---|---|---|
| `glutton-substrate-gvisor`, `glutton-substrate-microvm`, `openclaw-substrate-gvisor`, `openclaw-substrate-microvm`, `openclaw-agent-sandbox-gvisor` | 300 users on 300 workers, 4 min after a 1 min warm-up | 30 | The headline comparison; run every 6 hours |
| `glutton-agent-sandbox-gvisor` | 30 users on 30 workers, the most its pod-per-cycle keeps up with | 30 | Same, on agent-sandbox; run every 6 hours |
| `glutton-density`, `openclaw-density` | 100 workers kept at 80 active actors, with 100, 250 or 500 actors sharing them, against a never-suspend baseline | 10 | How many idle actors a node holds, and what sharing costs |
| `glutton-single`, `glutton-implicit`, `glutton-load`, `glutton-session`, `glutton-concurrent`, `glutton-shared-workers` | 10 users on one node, 5 min | 1 | One feature each: router wake, RAM and disk in the snapshot, five instructions per wake, four at once, workers shared by actors |
| `glutton-large` | 100 users over four load-generator pods, three repeats | 25 | Scale; run by hand, as it takes most of a region's C2 quota |
| `sandbox-benchmark` | 10 suspend-resume runs per provider | 1 | substrate against E2B, Modal, Daytona, Anthropic and OpenAI, with a monthly cost sheet |
| `dummy` | none | 0 | The Terraform path without a cluster, for checking a setup |

The file's comments document every key: the load, the cluster, repeats,
warm-ups, bare metal and the failure threshold (1% of cycles by default). Add a
case by adding an entry; see
[Adding a case](benchmarking/README.md#adding-a-case).

## Running a case yourself

You need a GCP project with C2 quota, Python 3.11, Terraform, gcloud, Go,
kubectl 1.31+ and Docker with Buildx; the full list and the cost of a run are
in [Prerequisites](benchmarking/README.md#prerequisites).

```bash
git submodule update --init
gcloud auth login && gcloud auth application-default login
cp benchmarking/.env.sh.example benchmarking/.env.sh   # set PROJECT_ID and the state backend
./benchmarking/tools/run-local-dummy                   # checks the setup without a cluster
./benchmarking/tools/run --scenario glutton-single     # about 30 minutes, most of it the cluster
```

A run checks everything the cases need before it applies anything, builds the
images of your tree that the repository lacks, applies the cluster, installs
the platform, runs the load, downloads the reports to `reports/<run-id>/` and
destroys the cluster. To iterate on a case without paying for a cluster each
time, keep one with `--dev-infra NAME`. To keep reports past the run, pass
`--report-bucket`, or send them to a BigQuery reports deployment of your own
with `--report-deployment`. All of it is in
[Running scenarios from your machine](benchmarking/README.md#running-scenarios-from-your-machine).

## The scheduled runs

Two Cloud Run jobs in the GCP project `akvelon-gke-aieco` (`us-central1`) run
the scenario-runner from the latest image of their branch. Which cases they
run is the suite of the same name in `scenarios.yaml`, so a case is added to
or dropped from a schedule by editing the suite, with no Terraform apply.

| Deployment | When (UTC) | Suite | What it runs |
|---|---|---|---|
| [`substrate-bench-auto`](automation-infra/deployments/substrate-bench-auto/README.md) | 03:00, 09:00, 15:00, 21:00 | `nightly` | The six 30-node cases above and `dummy`; about 3 hours |
| `substrate-bench-auto-cleanup` | 15:00 daily | | Destroys what a killed execution left |
| [`substrate-bench-staging`](automation-infra/deployments/substrate-bench-staging/) | 04:00 daily | `staging` | `dummy` and `glutton-single`, for trying changes to the job before `main` |

Each execution's reports land in `gs://substrate-bench-auto-tfstate/data/<run-id>/`
and are queryable in the BigQuery dataset `substrate_bench_auto`, for example:

```bash
bq query --project_id=akvelon-gke-aieco --use_legacy_sql=false \
  'SELECT run_id, test_case, p50_ms, p95_ms FROM substrate_bench_auto.locust_stats_steady WHERE name = "Resume" ORDER BY 1, 2'
```

To run the job now rather than waiting, or to run one case:

```bash
automation-infra/tools/trigger --deployment=substrate-bench-auto --scenario=glutton-substrate-gvisor
```

Checking an execution, charting its panels, pausing the schedule and
cleaning up after a failure are in the
[deployment's README](automation-infra/deployments/substrate-bench-auto/README.md).
Creating a deployment of your own is in the
[automation README](automation-infra/README.md).

## How a run is built

A push to `main` runs CI (gofmt, go vet and the Go tests; the Python tests;
shellcheck; terraform fmt and validate) and the Publish workflow, which builds
the job's runner image and the images a run pulls (substrate's, built with ko
from the submodule; the load generator; the workloads) into each deployment's
Artifact Registry repository, and moves the `runner:main` tag only once every
image a run needs is there. Images are keyed by the commit and the submodule
versions, so a run never mixes trees, and the preflight check refuses to start
a run whose images are missing.

## Layout

| Path | |
|---|---|
| [`benchmarking/`](benchmarking/README.md) | The scenario-runner (`scenario_runner/`), the cluster Terraform (`scenarios/infra/`), the cases (`scenarios/scenarios.yaml`), the load generator (`locust/`, Python master and Go boomer worker), the workloads and their images ([`workloads/`](benchmarking/workloads/README.md)), agent-sandbox's manifests (`agent-sandbox/`), and the provider comparison (`sandbox-benchmark/`, see its [doc](benchmarking/sandbox-benchmark/sandbox-benchmark.md)) |
| [`automation-infra/`](automation-infra/README.md) | The Terraform module for a Cloud Run job that runs the scenario-runner on a schedule, its image, the two deployments and the `trigger` and `destroy-leftovers` tools |
| [`benchmark-charts/`](benchmark-charts/) | Charts drawn from a run's raw CSVs, `pip install ./benchmark-charts` |
| `dependency-repos/` | Submodules the images are built from: `substrate` and its `env`, `agent-sandbox`, and `ai-sandbox-benchmark` for the provider comparison. Keep them at the pinned commits; the preflight check fails a run whose substrate is not |
| `reports/` | Where a run's reports are downloaded; ignored by git |
| `.github/workflows/` | `ci.yml` and `publish.yml` |

## Contributing

Open a pull request against `main`. CI runs the checks above; run them locally
with `go test ./...` in `benchmarking/locust/go`, the two `unittest` suites
`ci.yml` names, `shellcheck` and `terraform fmt -check -recursive`. A change to
a case or the runner is tried on the staging deployment first, then lands in
the nightly on the next push to `main`.
