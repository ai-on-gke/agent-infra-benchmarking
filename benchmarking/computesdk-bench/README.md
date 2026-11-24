# ComputeSDK Benchmarks (burst_TTI & DAX)

This suite integrates the `burst_TTI` and `DAX` benchmarks from ComputeSDK into the scenario-runner infrastructure.

---

## 1. Workloads

### Burst TTI (`benchmarks/sandbox/tti.bench.ts`)
Measures sandbox cold start and Time to Interactive under concurrent load:
- Creates a sandboxed actor in Substrate from the golden snapshot.
- Attaches an egress policy for outbound network access.
- Opens a local proxy port through `atenet-router` using `kubectl-ate proxy`.
- Probes `/readyz` for readiness.
- Runs the first command (`node -v`) over gRPC `ProcessService` (`guest.proto`).
- Measures `ttiMs` across iterations and concurrency levels, and generates `burst_tti.svg`.

### Heavy Real-World Build Workload - DAX (`benchmarks/sandbox/dax.bench.ts`)
Runs the full OpenCode build and compilation workflow inside a cold sandbox:
- Prepares environment and detects architecture.
- Clears package caches.
- Downloads Bun.
- Unpacks Bun.
- Clones OpenCode git repository.
- Runs `bun install` for workspace dependencies.
- Runs multi-project TypeScript typechecking (`bun typecheck`).
- Measures phase latencies, disk space consumption deltas, and generates `dax.svg`.

---

## 2. Running Scenarios

### On a Dev Infra (reusing an existing cluster)
```bash
./benchmarking/tools/run --dev-infra computesdk-dev --scenario burst-tti-gvisor
./benchmarking/tools/run --dev-infra computesdk-dev --scenario dax-gvisor
```

### On MicroVM (Cloud Hypervisor / Nested Virtualization)
```bash
./benchmarking/tools/run --dev-infra computesdk-dev --scenario burst-tti-microvm
./benchmarking/tools/run --dev-infra computesdk-dev --scenario dax-microvm
```

---

## 3. Artifacts and Reports

Results are collected and saved to `reports/<run-id>/<case>/`:
- `results.json`: Raw benchmark outputs and per-iteration timings.
- `computesdk_stats.csv`: Tabular metrics for BigQuery ingestion and comparisons.
- `burst_tti.svg` / `dax.svg`: Visual leaderboard charts.
- `runner.log`: Full stdout/stderr stream from `@benchsdk/runner`.
- `run.json`: Run metadata, commit provenance, and cluster configuration.
