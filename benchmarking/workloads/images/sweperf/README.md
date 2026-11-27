# SWE-perf Workload Actor

The actor container driven by the `sweperf` benchmark scenarios: an asynchronous HTTP execution engine ([`replay.py`](replay.py)) that replays pre-recorded SWE-bench coding agent trajectories in an isolated shell environment without requiring an actual LLM or external API credentials.

Each task's trajectory is partitioned into multiple suspend/resume cycles (default: 4 cycles). A virtual user resumes the actor, dispatches a cycle chunk via `POST /execute`, polls `GET /status?job_id=...` until completed, records in-container Command Execution Latency (CEL) and wall clock times, and suspends the actor. Once the full trajectory finishes, the actor is deleted and a fresh one created.

---

## Available Tasks and Traces

All traces live in [`traces/`](traces/):

| Task | Steps | Trace File | Default Cycles | Target Repository / Focus |
|---|:---:|---|:---:|---|
| `django-11099` | 15 | `traces/django-11099.json` | 4 | Django username validation |
| `astropy-7336` | 21 | `traces/astropy-7336.json` | 4 | Astropy coordinate transformations |
| `sympy-13647` | 33 | `traces/sympy-13647.json` | 4 | SymPy matrix manipulation |
| `matplotlib-20826` | 49 | `traces/matplotlib-20826.json` | 4 | Matplotlib axis tick visibility |
| `sphinx-8638` | 64 | `traces/sphinx-8638.json` | 4 | Sphinx autodoc instance attributes |

All 5 tasks share the single container image (`sweperf-workload`), which embeds all traces alongside `replay.py`.

---

## Running the Scenarios

Scenarios follow the naming pattern:

```text
sweperf-{task}-substrate-{sandbox-class}
```

- **`{task}`:** One of `astropy-7336`, `django-11099`, `matplotlib-20826`, `sphinx-8638`, `sympy-13647`
- **`{sandbox-class}`:** `gvisor` or `microvm`

### Examples

#### 1. Quick Single-Node Verification Run
Runs `astropy-7336` at 5 VUs on 5 workers on a single node without a warm-up:
```bash
./benchmarking/tools/run --scenario sweperf-single
```

#### 2. Running Individual Tasks on Substrate gVisor
```bash
# Django (15 steps)
./benchmarking/tools/run --scenario sweperf-django-11099-substrate-gvisor

# Astropy (21 steps)
./benchmarking/tools/run --scenario sweperf-astropy-7336-substrate-gvisor

# SymPy (33 steps)
./benchmarking/tools/run --scenario sweperf-sympy-13647-substrate-gvisor

# Matplotlib (49 steps)
./benchmarking/tools/run --scenario sweperf-matplotlib-20826-substrate-gvisor

# Sphinx (64 steps)
./benchmarking/tools/run --scenario sweperf-sphinx-8638-substrate-gvisor
```

#### 3. Running on Substrate MicroVM
Runs on Cloud Hypervisor guests using nested virtualization:
```bash
./benchmarking/tools/run --scenario sweperf-django-11099-substrate-microvm
```

#### 4. Reusing an Existing Development Cluster
To avoid creating and destroying a cluster for each run, use `--dev-infra`:
```bash
./benchmarking/tools/run --dev-infra <cluster-name> --scenario sweperf-django-11099-substrate-gvisor
```

---

## How It Works Under the Hood

1. **Image Build & Registry Push:**
   - The scenario runner checks [`benchmarking/workloads/images.json`](../../images.json).
   - If not already pushed, it builds `sweperf-workload` from this directory and pushes it to your project's regional Artifact Registry.

2. **ActorTemplate Deployment:**
   - Deploys the task manifest from `benchmarking/workloads/manifests/sweperf-{task}-template.yaml.tmpl`.
   - Replaces `${IMAGE_WITH_DIGEST}` with your pushed image.
   - Configures the entrypoint command: `["python3", "/replay.py", "/traces/{task}.json"]`.

3. **Locust & Boomer Load Generation:**
   - Master pod runs Locust with `user-class: sweperf` ([`benchmarking/locust/master-image/tests/sweperf.py`](../../../locust/master-image/tests/sweperf.py)).
   - Worker pod runs Boomer Go worker ([`benchmarking/locust/go/cmd/boomer-worker/main.go`](../../../locust/go/cmd/boomer-worker/main.go)) with:
     - `--sweperf-template=sweperf-{task}`
     - `--sweperf-total-steps={steps}`
     - `--sweperf-num-cycles=4`
     - `--sweperf-poll-interval-ms=100`

---

## Reported Metrics

Outputs are written to `reports/<run-id>/<case>/`:

| Metric | Description |
|---|---|
| `TaskWallClock` | Total client wall clock for one complete task trajectory (all cycles, excluding inter-cycle think time). |
| `TaskCEL` | In-container Command Execution Latency (CEL) summed over all cycles of one complete task. |
| `CycleCEL` | In-container Command Execution Latency (CEL) for one individual cycle chunk. |
| `ResumeToFirstExec` | Time from `ResumeActor` start until the sandbox answers the cycle's `/execute` request. |
| `ResumeActor` | Server-side elapsed time for the `ResumeActor` control-plane wake RPC. |
| `SuspendActorAsync` | Server-side elapsed time for the `SuspendActor` snapshot and background upload RPC. |
| `CreateActor` | Server-side latency to register and allocate the actor container. |
| `DeleteActor` | Server-side latency to terminate and unregister the actor container. |
| `Workload_Cycle_<n>` | Client round-trip time for cycle $n$ (`POST /execute` + `/status` polling). |
