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
- Go and git
- kubectl 1.31 or later
- gcloud's `gke-gcloud-auth-plugin` (`gcloud components install
  gke-gcloud-auth-plugin`), which kubectl reaches the clusters through
- Docker with Buildx, for building the images, by `tools/build-images` or a
  run: the image builds use `--build-context`
- curl and a `tar` with zstd support, for cases that install substrate with
  `sandbox-class: microvm`

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
  grep -E "^(METRIC|C2_CPUS|N2_CPUS) "
```

Short of C2, set `default-machine-type` and `sandboxed-machine-type` in the
case's `infra.cluster` to another family, such as `n2-standard-16` (which the
`microvm` class also runs on).

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

To run a case:

```bash
./benchmarking/tools/run --scenario glutton-gvisor
```

A run pulls its images from a repository and first builds the images of your
tree that it lacks, so the first run of a tree takes longer. Without a
repository, it applies one of its own and destroys it after its clusters, so
every such run builds every image again. To keep the images between runs,
point it at a repository that lasts, such as automation-infra's deployment's
(its `image_repo` output): set `IMAGE_REPO` in `benchmarking/.env.sh`, or
pass `--image-repo`. A dev infra has a repository of its own.

To build the images into a repository ahead of a run, without running
anything, only the ones it lacks being built:

```bash
./benchmarking/tools/build-images --repo=us-central1-docker.pkg.dev/<project-id>/<repository>
```

To run more than one case, repeat `--scenario`; they run in the order given.
`--all` runs every case in `scenarios.yaml`, in the order listed; one or the
other is required, as every case means a cluster or more. A case's
`repeats` say how many times it runs and on how many clusters (see
[Repeats](#repeats)). A failed repeat doesn't stop the ones after it.

Before it applies anything, the run checks what the cases need, and names
every problem it finds: the tools and their versions, Application Default
Credentials and gcloud's login, the substrate submodule checked out at the commit the repo pins, and for each case
its cluster's config as Terraform evaluates it, such as a machine type that
can run `microvm`, a mock-llm node pool for `openclaw` and a GKE version the
REGULAR channel still offers.

Without `--report-bucket`, each case's reports go to its cluster's own bucket,
which is destroyed with the cluster; the only copy that lasts is the one
downloaded to `reports/<run-id>/`, and a repeat whose download fails fails.
To keep them in GCS, pass a bucket of your own with `--report-bucket`.

Creating and destroying a cluster takes most of a run's time. While you are
changing a case or the code it runs, keep one cluster up instead:

```bash
./benchmarking/tools/run --dev-infra glutton-dev --scenario glutton-gvisor
```

See [Keeping a cluster between runs](#keeping-a-cluster-between-runs) for
reusing it and destroying it afterwards.

A run's reports are kept only in `reports/` once its cluster is gone. To query
your runs in BigQuery instead, send them to a reports deployment of your own;
creating one is in
[Keeping your own runs' reports in BigQuery](../automation-infra/README.md#keeping-your-own-runs-reports-in-bigquery):

```bash
./benchmarking/tools/run --report-deployment automation-infra/dev/reports --scenario glutton-gvisor
```

Useful flags (`--help` lists them all):

| Flag | |
|---|---|
| `--scenario NAME` | case to run; repeatable |
| `--all` | run every case instead |
| `--name RUN_ID` | run id, default `run-<UTC timestamp>`. It starts the names of the GCP resources, so keep it lowercase |
| `--project-id ID` | overrides `PROJECT_ID` |
| `--region REGION` | default `us-central1` |
| `--state-backend local\|gcs` | overrides `STATE_BACKEND`, see [Terraform state](#terraform-state) |
| `--report-deployment DIR` | send the reports to the bucket of that applied reports deployment, such as `automation-infra/dev/reports`, to be queried in its BigQuery dataset |
| `--image-repo REPO` | overrides `IMAGE_REPO`, the repository the images are pulled from |
| `--bare-metal`, `--no-bare-metal` | run every case's workers on bare metal, or on VMs, whatever the cases say; see [Running on bare metal](#running-on-bare-metal) |
| `--scenarios-file PATH` | another scenarios file, e.g. a local copy with a smaller load |
| `--report-bucket BUCKET` | keep the reports in this bucket, under `--report-prefix` (`reports` by default), rather than in each cluster's own |
| `--terraform-output json` | log Terraform as JSON |

`tools/run-local` is `tools/run --state-backend local`.

Substrate blocks all outbound traffic from an actor that has no egress policy.
The worker gives each actor one that allows only the hosts named by URLs in
its ActorTemplate's env, such as the OTLP collector and openclaw's mock LLM:
over cleartext HTTP for `http://` URLs, and over TLS, passed through
undecrypted, for `https://` ones, which a cleartext rule would not let
through. To let actors reach another service, add its URL to the template, or
set the case's `worker: actor-egress-allow` (cleartext) or
`worker: actor-egress-allow-tls` (TLS) to a comma-separated list of hosts.

### What a run does

1. Checks what the cases need, as above, the images in `--image-repo`
   included: substrate's from the submodule, and the locust and workload
   images (`locust/images.json`, `workloads/images.json`), which every
   repeat's cluster pulls.
2. For each repeat of each case:
   1. Applies the case's cluster from `scenarios/infra/clusters/<name>/`: a
      zonal GKE cluster whose nodes have no public IP and reach out through
      Cloud NAT, its node pools, a GCS bucket for the reports and the IAM the
      benchmark needs. From a laptop, the cluster gets a network of its own
      and a public control-plane endpoint, which IAM guards; from
      automation-infra's Cloud Run job, it goes into the job's network with an
      internal endpoint only.
   2. Installs what the `install` in the case's type's mapping names,
      substrate or agent-sandbox, if either; substrate with `sandbox-class:
      microvm` also its kata and cloud-hypervisor assets, checked against the
      sha256s the SandboxConfig pins before they are uploaded.
   3. Deploys Prometheus, the worker pool and the case's workload, then the
      locust master and boomer worker Jobs, and waits for both to end.
   4. Deletes any actors the run left, recording them in `cleanup.json`,
      uploads the Jobs' logs to the bucket beside the master's CSVs, and
      copies everything to `reports/<run-id>/<record>/`.
   5. Destroys the cluster, and its bucket with it.

The `common` cluster's nodes are C2 by default; see
[Cost and quota](#cost-and-quota) before raising `sandboxed_node_count`.

### Where things end up

| What | Where |
|---|---|
| Reports and Job logs | `reports/<run-id>/<record>/` at the repo root, and with `--report-bucket` or `--report-deployment` in that bucket. The cluster's own bucket is destroyed with it |
| Terraform deployments | `scenario_runner/.local-data/runs/terraform/<run-id>/`: `scenarios/<record>/`, each with `config/`, `tf-data/` and, with the local backend, `tfstates/` |
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

### What a run's reports hold

Each folder under `reports/<run-id>/` holds:

- `locust_stats_steady.csv`: the run's latency percentiles over the
  steady-state window; see [Reading the results](#reading-the-results).
- `locust_stats.csv`, `locust_stats_history.csv`, `locust_failures.csv`,
  `locust_exceptions.csv`: locust's own output, over the whole run.
- `custom_stats_history.csv`, `cluster_vitals_history.csv`: actor counts and
  node CPU and memory over the run, read back out of Prometheus.
- `locust_steady_window.json`: the steady-state window's bounds.
- `run.json`: how the run went (its failed cycles, over the whole run and the
  steady state), the resolved case, the cluster and its nodes, the
  WorkerPool, ActorTemplate and SandboxConfig, every image by the digest its
  pods ran, substrate's control plane in `ate-system` included, the commits
  and whether the tree had uncommitted changes, and the versions of the tools
  the run had. `README.md` is the same for a
  reader, with a results table.
- `locust-master.log`, `boomer-worker.log`: the Jobs' logs, every pod's,
  taken from the pods once the run ends. A run stopped with Ctrl-C has none,
  as stopping it deletes the pods.
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

### Images and versions

`tools/build-images`, and a run that builds them itself, builds the images
from the working tree as it is, and
tags them with the repo's `HEAD` commit; a tree with uncommitted changes, here
or in the submodules, gets `<commit>-dirty-<digest of the changes>` instead, so
an image is only ever reused for exactly the code it was built from, and
`run.json` records which. Commit before a run whose results you want to tie to
a commit. Substrate's, env's and agent-sandbox's images are tagged with their
submodule's version instead, below; a `-dirty` one is built again every time. The images
are built for `linux/amd64`, the nodes' platform, whatever the build host's;
on an ARM machine that needs Docker's emulation (with Rosetta on Apple
silicon).

Substrate is built from `dependency-repos/substrate`, which has to be checked
out at the commit the repo pins: after a pull, run `git submodule update`. To
benchmark another substrate commit, check it out there and `git add` the
submodule. Its version is `git describe --tags --always --dirty` there, or
`VERSION` if set, and the cluster's nodes are labeled with it.

The GKE minor version is pinned (`kubernetes_version` in the cluster config,
1.35), and GKE picks a build of it that the REGULAR channel offers; each run's
`run.json` and `README.md` record the build it got. The cluster's maintenance
exclusion keeps it on that minor, and its node pools on their build, for the
life of the cluster. An exact build can be pinned too, but REGULAR drops each
build within weeks. When REGULAR does not offer the pin where the cluster goes
(its zone, or its region for a regional control plane, which can differ), a
run refuses to start and names the versions REGULAR offers there.

### Terraform state

With `local`, each deployment's state is a file under
`.local-data/runs/terraform/<run-id>/`, so only this machine can destroy what
a run left behind. With `gcs`, it is in `gs://<state bucket>/<state prefix>/<run-id>/`
together with a copy of the rest of the deployment, so a destroy needs nothing
but the bucket. Use `gcs` for long runs, or when several people share the
project.

### When a run fails or is stopped

A run fails when more than its case's `locust.max-failure-ratio` of its cycles
fail (0.01 by default), over the whole run. A cycle fails on any of its calls,
and counts once however many rows that failure shows in; an actor that could
not be created counts as a failed cycle too. A run also fails when it completes no
cycle, or when the load generator raises any exception
(`locust_exceptions.csv`). A failed case does not stop the ones after it; the
execution exits non-zero at the end, naming them.

A run that may still be going, because it outlived the hour the
scenario-runner waits for it or its Jobs' status could not be read for ten
minutes, fails its repeat. Its cluster is destroyed with it, and the series goes
on, except on a dev infra, where the next run would deploy under it, so the
series stops.

A failed apply, install or benchmark still destroys the repeat's cluster. If
that destroy fails, the run says what failed before it too, and stops the
series rather than apply more.

Ctrl-C stops the run in progress the way its end would: the master stops the
worker, which deletes its actors, and uploads what it recorded, and the
reports are downloaded before the cluster is destroyed. The destroy then runs
out of the terminal's reach: another Ctrl-C only warns, and a second within
five seconds of it interrupts Terraform, which leaves behind whatever it had
not destroyed yet.

If a destroy itself fails, the resources stay. Run it again by hand from the
deployment's `config/`:

```bash
cd benchmarking/scenario_runner/.local-data/runs/terraform/<run-id>/scenarios/<record>/config
TF_DATA_DIR=../tf-data terraform destroy
```

Anything still left can be found in the console by the `benchmark-run-id`
label.

### Keeping a cluster between runs

To iterate without creating a cluster on every run, use `--dev-infra NAME`. It
keeps an image repository and a cluster between executions, and repeats run
one after another on that cluster. Each scenario needs its own `--dev-infra`
name, because a dev infra keeps the cluster of the case it was first created
for. The first run applies it, builds the images into its repository and runs
the case; later runs reuse both:

```bash
./benchmarking/tools/run --dev-infra glutton-dev --scenario glutton-gvisor
```

After changing the code, the next run builds the images of the changed tree
before it starts. To reuse the workloads already deployed, or reapply the
cluster config after changing it, reinstalling substrate if its version
changed:

```bash
./benchmarking/tools/run --dev-infra glutton-dev --scenario glutton-gvisor --no-deploy-workloads
./benchmarking/tools/run --dev-infra glutton-dev --scenario glutton-gvisor --dev-infra-update
```

A dev infra's sandboxed node pool is applied for one sandbox class, so a case
of the other needs a dev infra of its own.

A dev infra's state is always local, in
`scenario_runner/.local-data/dev-infra/terraform/<name>/`, and its kubeconfig
in `cluster/tf-data/kubeconfig` there. `tools/dev-infra` manages them:

```bash
# every dev infra on this machine, its case, project and whether it is complete
./benchmarking/tools/dev-infra list
# its Terraform outputs: cluster name, bucket, image repository
./benchmarking/tools/dev-infra outputs glutton-dev
```

A dev infra keeps costing money until it is destroyed, so destroy it once you
are done iterating. This destroys its cluster and image repository and removes
its local files:

```bash
./benchmarking/tools/dev-infra destroy glutton-dev
```

### Adding a case

Add an entry under `cases:` in `scenarios.yaml`. The comments at the top of that
file describe each key. To change the load or the worker pool, add a new case
rather than editing an existing one, so results stay tied to the case name
they were recorded under. To compare gVisor and microvm, add both, the same
but for `locust.install.substrate.sandbox-class` and the microvm one's
`infra.cluster.nested-virtualization: true`, which off bare metal gives its
guests `/dev/kvm`.

Besides the load, a case can shape each cycle with the worker's options:

| `locust.worker` key | What each cycle does |
|---|---|
| `resume-mode: implicit` | no `ResumeActor`: the instruction wakes the actor through the router (`glutton-implicit`) |
| `instructions-per-cycle: N` | N instructions between the resume and the suspend, the first closing TTFI (`glutton-session`) |
| `concurrent-instructions: N` | N instructions at once at each of those steps (`glutton-concurrent`) |
| `glutton-write-ram: SIZE`, `glutton-write-disk: BYTES` | rewrites that much of the actor's memory and a file on its disk before the suspend, as `WriteRAM` and `WriteDisk` rows, so the snapshot holds changed state (`glutton-load`, with `actor-memory: 512Mi`) |

In the case's `locust`, `actor-memory` sets the actors' memory limit, which
on `microvm` is the guest's RAM, and `locust-workers` runs that many boomer
worker pods, each with its own connections to substrate, for loads one pod
cannot drive (`glutton-large`).

The scheduled runs (`automation-infra/deployments/substrate-bench-auto`) run
the cases its `main.tf` lists in `args`, so a new case runs there once it is
added to that list.

### Running on bare metal

A case with `bare-metal: true` runs its `sandboxed` pool on a bare-metal
machine rather than a VM, so gVisor runs without a hypervisor under it and
microvm boots its guests on the host's own `/dev/kvm`, without nested
virtualization. It is recorded as `<case>-metal`, so its results never mix
with the case's on VMs; `run.json` names the machine type and zone each node
had. A case that does not say takes `bare-metal.default` at the top of
`scenarios.yaml`, false as shipped, and `--bare-metal` or `--no-bare-metal`
overrides both for one run:

```bash
./benchmarking/tools/run --bare-metal --scenario glutton-microvm
```

GKE takes only C3's `-metal` machine types (`c3-standard-192-metal`,
`c3-highcpu-192-metal`, `c3-highmem-192-metal`), which boot from Hyperdisk;
any `-metal` `sandboxed_machine_type` gets a `hyperdisk-balanced` boot disk.
Each is a whole host of 192 vCPUs, about ten times a `c2-standard-16` an hour,
and zones run out of them often. So the pool goes on the first of
`bare-metal.placements` in the run's `--region`, a zone and a machine type
each, and the whole cluster with it. If that zone is out of the machine type,
the run destroys what it applied there and moves on to the next placement,
wrapping around; the last it tries is retried like any apply. GKE takes about
35 minutes to give up on a pool it cannot get nodes for, so the cases after it
start from the placement the last one got rather than the first. The preflight checks that each placement's
zone offers its machine type and the GKE version, and that the pool fits in
the region's C3 CPU quota. A 300-vCPU quota, for one, fits a single node:

```bash
gcloud compute regions describe us-central1 --project <project-id> \
  --flatten=quotas --format="table(quotas.metric,quotas.usage,quotas.limit)" |
  grep -E "^(METRIC|C3_CPUS) "
```

A dev infra keeps the placement it got, and runs only cases on the same kind
of pool as its first.

### Comparing sandbox providers' costs

The `sandbox-benchmark` case measures substrate's, E2B's, Modal's and
Daytona's suspend and resume overheads and writes a monthly cost sheet next to
Anthropic Managed Agents and OpenAI. It needs the providers' API keys; see
[sandbox-benchmark.md](sandbox-benchmark/sandbox-benchmark.md):

```bash
./benchmarking/tools/run --dev-infra cost-dev --scenario sandbox-benchmark
```

## Reading the results

The committed example is run
[`reports/run-20261015t144424z/`](../reports/run-20261015t144424z/): the
shipped cases `glutton` and `glutton-microvm`, three repeats each, and
`openclaw` and `openclaw-microvm` once, each repeat on a cluster of its own as
`tools/run` applies them. It was recorded from the change that added it,
before that was committed, so its `run.json` files name commit 1aa312c with
uncommitted changes. Each case's `<case>-summary.md` has its numbers over the
repeats, the hardware and the versions; each repeat's folder has its own
`README.md`.

`glutton` and `glutton-microvm` differ only in their sandbox class: 10 users on
10 workers on one `c2-standard-16` sandboxed node, for 5 minutes after a 10 s
warm-up. In steady state, the median over the three repeats, in ms:

| Case | TTFI p50 | TTFI p99 | Suspend p50 | Suspend p99 | Cycles/s |
| --- | --- | --- | --- | --- | --- |
| `glutton` (gVisor) | 310 | 1300 | 520 | 1600 | 8.5 |
| `glutton-microvm` | 960 | 1400 | 1400 | 3800 | 3.7 |

A microvm actor takes about three times as long to resume and to suspend, so
each user completes fewer cycles. The sandboxed node's CPU stayed at or below
55% in both (`cluster_vitals_history.csv`), so the difference is the sandbox's
own rather than contention.

`glutton-large`, run
[`reports/run-20261015t192645z/`](../reports/run-20261015t192645z/), is
`glutton` at ten times the load: 100 users on 100 workers over 25 sandboxed
nodes, with the users spread over four boomer pods. Over its three repeats,
TTFI p50 is 240 ms (240-260) and p99 1000 ms (590-1500), Suspend p50 440 ms and
p99 2400 ms (1300-3200), at 98 cycles/s, with 2 of 83,741 cycles failed. The
sandboxed nodes stayed below a quarter of their CPU.

### What each row measures

The worker takes each actor through resume, one instruction and suspend, and
times the pieces. The request names in the CSVs are:

| Name | What it times |
| --- | --- |
| `TTFI` | Time to first instruction: from the start of the `ResumeActor` (explicit mode) or of the instruction that wakes the actor (implicit mode) until that instruction has been served and its answer checked. It includes the instruction's own service time. |
| `ExecutionTime` | Each instruction's round trip to the actor on its own. |
| `CycleTime` | The whole cycle: `TTFI`, any further instructions and load, and then the suspend. |
| `WriteRAM` / `WriteDisk` | A `glutton-write-ram` / `glutton-write-disk` cycle's load, before its suspend. |
| `Resume` / `Suspend` | The `ResumeActor` / `SuspendActor` calls on their own. |
| `CreateActor` / `CreateActorEgressPolicy` / `CreateAtespace` / `DeleteActor` | The lifecycle calls. `CreateAtespace` is once per run; `DeleteActor` is mostly the teardown. |
| `ActiveActors` / `IdleActors` | Gauges in `custom_stats_history.csv`: actors resumed and actors suspended. |

Every call has a deadline, the case's `workload-timeout`; a call past it
counts as failed.

### Which file is authoritative

`locust_stats_steady.csv` is the number to quote for a repeat. It comes from
locust, which counts failed requests too and keeps response times at 1 ms
resolution below 100 ms, 10 ms below 1 s, 100 ms below 10 s and 1 s above.
The worker reports each latency to locust rounded to the nearest whole
millisecond, which is all locust takes, so a call under a millisecond shows as
0 or 1 ms.

`custom_stats_history.csv` is read back out of Prometheus. The worker's
histograms there use power-of-two buckets (1, 2, 4, ... 32768 ms) and count
successes only, so they show a curve's shape, not an exact tail. Where it and
locust disagree, locust wins.

### Steady state, and what the p99 is made of

- **The ramp is excluded.** `locust_stats.csv` covers the whole run, the
  ramp included. `locust_stats_steady.csv` starts the case's `warm-up` after
  the last user has started; a case without `warm-up` writes none.
- **The p99 is rare stalls.** A repeat's steady-state window holds about
  2,400 cycles of `glutton` and 1,050 of `glutton-microvm`. Their p50 barely
  moves between repeats (`glutton`'s TTFI 300-310 ms), but a few cycles stall
  for seconds, and how many land in a window decides the p99: `glutton`'s TTFI
  p99 is 820-1700 ms over the example's repeats, and once a Resume ran into the
  30 s deadline and failed its cycle. Quote p50, and give the p99 with its
  range over the repeats, which the summary has, rather than one repeat's.

### Comparing sandboxes with openclaw

An openclaw instruction is a whole agent turn: openclaw runs its tool-calling
loop against mock-llm, and `ExecutionTime` times it, about 25 s on gVisor and
41 s on microvm in the example. That turn makes up nearly all of `TTFI` and
`CycleTime`, and only a handful of cycles fit in a run, so compare gVisor and
microvm on `Resume` and `Suspend`, as the run's `README.md` says for openclaw
cases. In the example, `openclaw` resumed in 620 ms and suspended in 1300 ms
(p50 of 12 cycles), and `openclaw-microvm` in 6900 ms and 6400 ms (p50 of 6).
A turn counts only if its output carries the nonce the worker put in its
prompt, which mock-llm repeats in its last answer.

### The load generator

A case's users run in one boomer worker pod unless it sets `locust-workers`,
each pod with its own connections to substrate (one by default, more with the
worker option `api-connections`). The shipped cases run 10 users in one pod,
and `glutton-large` 100 over four; for more users, split them over more pods
and check that their CPU stays well below their nodes' in
`cluster_vitals_history.csv`.

### Repeats

One run is one sample. Compare cases across their repeats, not by a single
one: `<case>-summary.csv` gives the min, median and max of each stat over
them, as the example's `glutton-summary.csv` does. A case's `repeats` says how
it is sampled:

```yaml
repeats:
  count: 3          # measured runs, recorded as <case>-1 to <case>-3; default 1
  warmups: 1        # runs before them that are not measured; default 1
  cluster: shared   # default; or per-repeat
```

By default a case's runs go one after another on one cluster applied for it,
the warm-ups first, taking the cold start: image pulls, node scale-up,
substrate's first snapshots. A warm-up is recorded as `<case>-warmup-<n>`, so
BigQuery's `scenario` for it is `<case>-warmup`, and is left out of the
summary; one that fails only warns. With `cluster: per-repeat`, each measured
run gets a cluster of its own and there are no warm-ups. Repeats on one
cluster are tighter but less independent, as they leave out how one cluster
differs from the next, so compare cases sampled the same way. On a
`--dev-infra`, every run is on its cluster either way.
