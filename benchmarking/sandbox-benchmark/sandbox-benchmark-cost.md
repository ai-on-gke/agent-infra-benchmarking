# Sandbox cost sheet

The `sandbox-benchmark-cost` case measures how long E2B, Modal and Daytona
sandboxes take to suspend and resume, and turns that into a monthly cost sheet
next to Anthropic Managed Agents and OpenAI. To produce one, from the repo root:

```bash
./benchmarking/tools/run --dev-infra cost-dev --scenario sandbox-benchmark-cost
```

The sheet is then at `reports/<run-id>/sandbox-benchmark-cost/cost_sheet.csv`,
in the same layout as the original cost spreadsheet, ready to import into
Sheets.

## Before the first run

Do the [setup](../README.md#setup) of the scenario-runner first. Then add the
providers' keys to `benchmarking/.env.sh`, which the runner hands to the
comparator in a Secret:

```bash
export E2B_API_KEY=...
export DAYTONA_API_KEY=...
export MODAL_TOKEN_ID=...
export MODAL_TOKEN_SECRET=...
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...
```

## Running it

The command above runs the case on a dev infra named `cost-dev`, a cluster kept
between runs; the first run creates it, which takes longer. Give it a name of
its own: a dev infra keeps the cluster of the case it was created for (see
[Keeping a cluster between runs](../README.md#keeping-a-cluster-between-runs)).
Without `--dev-infra`, the runner applies a cluster for the run and destroys it
afterwards.

On an existing dev infra a run takes about 15 minutes. The sandboxes cost
cents; most of the cost is Anthropic's and OpenAI's model tokens.

## What a run produces

Everything lands in `reports/<run-id>/sandbox-benchmark-cost/`, and in the
cluster's bucket under the same path:

| File | What it holds |
|---|---|
| `cost_sheet.csv` | The cost sheet |
| `perf_results.csv` | Every metric's mean, P90 and std dev per provider: creation, suspend, resume, execution, cleanup, ready to import into Sheets |
| `<provider>/` | Each provider's own reports: `results.json` with its raw timings, its comparator log and `run.json` |

## How the numbers are made

Each provider runs ai-sandbox-benchmark's `test_suspend_resume` 10 times, after
one warmup run. The test sets up a small agent workspace in the sandbox (a git
repository, 50 Python modules, a venv with pydantic, fastapi and pytest),
suspends the sandbox, resumes it and checks the workspace survived.

- Suspend is timed until the provider's call returns: E2B's pause, Modal's
  snapshot and terminate, Daytona's stop.
- Resume is timed until the resumed sandbox has run a command, since E2B's and
  Modal's calls return before it is usable.
- All three sandbox providers run 2 vCPU and 2 GiB: E2B's default sandbox, and
  sizes set explicitly for Modal and Daytona in their providers in
  `dependency-repos/ai-sandbox-benchmark/providers/`.
- Anthropic and OpenAI cannot suspend or resume, so their overheads are N/A.

The sheet takes each provider's mean suspend and resume time as the overhead
of one suspend and resume per hour, adds it to 360 active seconds an hour,
prices that time and multiplies by 270 hours a month.

## Changing prices or assumptions

The prices and usage assumptions are constants at the top of
[`sandbox_benchmark_cost_sheet.py`](../scenario_runner/src/scenario_runner/sandbox_benchmark_cost_sheet.py):
`RESOURCE_PRICED` and `ANTHROPIC_SESSION_HOUR` for prices,
`ACTIVE_SECS_PER_HOUR`, `HOURS_PER_MONTH`, `VCPUS` and `RAM_GIB` for usage. They
are copied from the original sheet, not fetched live.

The providers, number of runs and warmups are the case's, in
[`scenarios.yaml`](../scenarios/scenarios.yaml). Any case of type
`sandbox-benchmark` with `cost-sheet: true` gets a cost sheet.

## Reproducing a run

Each provider's `run.json` records what ran: this repo's `commit` and whether
it had uncommitted changes (`commit_dirty`), `ai_sandbox_benchmark_commit` and
the comparator image's digest. Checking out those commits runs the same code
on the same sandbox sizes. Push the ai-sandbox-benchmark submodule before
pushing a change to its pointer, or CI cannot check it out.

The numbers themselves will differ between runs: the providers' hardware and
load change, and E2B's template, Modal's base image, Daytona's image and the
workload's pip packages are not pinned. Suspend times vary by about 25% between
runs; the monthly totals stay within a cent.

## Caveats

- Anthropic's cost is the session fee only; model tokens are not included.
- OpenAI publishes no price for its Agents API sandboxes.
- Modal runs `cpu=2.0`, since `cpu=1.0` measured under 2 vCPU. Modal bills per
  core; if it counts a core as 2 vCPU, its vCPU cost in the sheet is half of
  what the sandbox is billed.
- Storage for snapshots and suspended sandboxes is not included.
