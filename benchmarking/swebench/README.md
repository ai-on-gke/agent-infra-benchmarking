# swebench: sandboxenv-rl's loader as a case

A case of type `swebench` runs `swebench-loader` of
[sandboxenv-rl](https://github.com/ai-on-gke/sandboxenv-rl) on a cluster the
scenario-runner makes, so it gets the cluster's lifecycle, the images and the
reports the other cases have. The loader plays an RL trainer with no model: it
makes environments through sandboxenv-rl's `ate-env-api`, runs a workload in
each, parks and wakes them between training steps, deletes them, and records
how long each operation took, as a trainer sees it.

The locust cases time substrate's own calls, resume and suspend, with the
platform's synthetic workloads. This one times what an RL job gets through
sandboxenv-rl's API on top of substrate, with task images such as SWE-bench's:

| Question | Run shape |
|---|---|
| How long does a sandbox whose image is on no node take to run its first command? | `mode: eval`, one task per image or many, on nodes that have not seen the images |
| And when the image is on the node already? | `mode: eval` with several `tasks-per-image` on a fixed pool |
| What does it cost to park a fleet between training steps and wake it for the next? | `mode: steps` with a `park`, such as `suspend` |

`swebench-loader`'s own page,
[`dependency-repos/sandboxenv-rl/benchmarking/swebench/README.md`](../../dependency-repos/sandboxenv-rl/benchmarking/swebench/README.md),
says what each of its modes and numbers is.

## What a run does

1. Installs substrate on the case's cluster, with the SPIFFE ID of `ate-env-api`
   in the router's allow-list (`ingress_spiffe_ids` of the case), as `ate-env-api`
   dials the router over mTLS.
2. Deploys sandboxenv-rl from `dependency-repos/sandboxenv-rl`, from the images
   `tools/build-images` builds of it: the atespace and its access policy, the
   token `ate-env-api` requires, `ate-env-api` itself, a WorkerPool of
   `env-worker-count` workers, and the `default-template` ActorTemplate, whose
   golden snapshot it waits for. The module is
   [`sandboxenv_rl.py`](../scenario_runner/src/scenario_runner/benchmarks/swebench/sandboxenv_rl.py).
3. Makes the dataset the ConfigMap of the loader's Job, and copies the token to a
   Secret in the Job's namespace.
4. Runs the loader as one Job, [`manifests/loader-job.yaml`](manifests/loader-job.yaml),
   whose pod runs [`image/run_loader.py`](image/run_loader.py): it runs the loader,
   summarizes its events, and uploads the files to the case's report folder, as
   the Job's service account may create objects in the bucket.
5. Deletes the Secret and the ConfigMap, deletes the actors the run left, and
   downloads the reports to `reports/<run-id>/<case>/`.

## Running it

```bash
git submodule update --init
./benchmarking/tools/run --dev-infra swebench-dev --scenario swebench-substrate-gvisor
```

The first run builds the images of this tree that the repository lacks, among
them the loader's and sandboxenv-rl's. The case is in no suite yet, so no
schedule runs it. A dev infra keeps the router allow-list it was installed with
and installs substrate again to widen it, so a dev infra first made for another
case works too.

## The dataset

`dataset` is one of:

- `images`, names of the task images in [`images.json`](images.json). The loader
  runs its built-in `workload-file-read-one` workload in them: it runs `true` and
  reads the `read-file` (`/etc/os-release` by default). It has nothing to grade, and the
  case needs no SWE-bench images in a registry. `swebench-smoke-rootfs` is a
  plain Debian userland built by this repository.
- `file`, the path in this repository of a manifest that sandboxenv-rl's
  `make-dataset.py` made, whose images are in a registry the cluster can pull
  from (`mirror-images.sh` copies them). The manifest is the Job's ConfigMap, which
  holds a megabyte, so a case refuses a file over 900 000 bytes: make the
  manifest of fewer instances, or take a subset with `max-images`.

  It needs `pip install datasets 'swebench>=5'`, and crane for the image sizes:

  ```bash
  python3 dependency-repos/sandboxenv-rl/benchmarking/swebench/make-dataset.py \
    --registry us-central1-docker.pkg.dev/PROJECT/swebench --limit 50 \
    --out benchmarking/swebench/datasets/swebench-50.jsonl
  ```

  With `file` the loader's own default workload, `swebench-gold`, replays each
  instance's gold patch and grades it, so a run also checks correctness; `trajectory`
  names another.

Any other flag of `swebench-loader run` goes in `loader-args`, except the ones the
case's own keys or the run set, which a case refuses.

## What a run's reports hold

In `reports/<run-id>/<case>/`, and in the bucket under the same path:

| File | What it holds |
|---|---|
| `events.jsonl` | One event per operation, with the time it took: the source of truth |
| `stats.jsonl` | Flat `{timestamp, tag, test_name, metric, measurements}` rows of the run, for tables and dashboards |
| `run_window.json` | The run's id, name, tag and start and end |
| `summary.json`, `report.txt` | `swebench-loader report` of the events: counts, failures and the percentiles of each operation |
| `run.json` | What ran: the case, its loader arguments, the dataset's digest, the cluster, the images by digest, the commits of this repo, substrate and sandboxenv-rl, and the loader's exit status |
| `cleanup.json` | The actors the run left in its atespace, and what became of them |
| `swebench-loader.log` | The loader's log |

A failed run keeps its files too. `stats.jsonl` and `events.jsonl` are not in the
BigQuery views of the scheduled runs yet.

## Reading the numbers next to the locust cases'

They are measured by different tools, through different calls, so one name is
not one number. The locust cases' `TTFI` is timed by the boomer worker from the
start of the resume, through substrate's API, to the checked answer of its
instruction; the loader's `ttfic` is timed from the ask through `ate-env-api` to
the first command's answer, with the API server and the network in it. Compare
the loader's numbers with each other, across clusters and sandbox classes, and
with the locust cases' only as a rough estimate of what `ate-env-api` adds, when
both run on the same cluster shape.

## The sandboxenv-rl submodule

`dependency-repos/sandboxenv-rl` is pinned like substrate: a run refuses to
deploy it from a commit the repo does not pin, `git submodule update` checks the
pin out, and to benchmark another commit you check it out there and `git add` the
submodule. Its images are tagged with `git describe` of it.

The `sandbox-benchmark` case still deploys `dependency-repos/env`, which is
agent-substrate/env, sandboxenv-rl's origin. Its provider, in the
`ai-sandbox-benchmark` submodule, installs that env's Python client, `ate_env`,
at the commit it pins in `ENV_COMMIT`, and `sandboxenv-rl` renamed that client
`ate_env_rl` and made `ate-env-api` require a token. Moving `sandbox-benchmark`
over takes a change to that provider first; `sandboxenv_rl.py` is what it would
deploy.
