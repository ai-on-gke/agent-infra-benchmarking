# agent-infra-benchmarking

Benchmarks for agent infra offerings, starting with
[substrate](https://github.com/agent-substrate/substrate) on GKE.

| Part | |
|---|---|
| [`benchmarking/`](benchmarking/README.md) | The scenario-runner. It creates a GKE cluster for each benchmark case, installs substrate on it, runs a locust load test against it and destroys it afterwards. Start here to run benchmarks from your machine. |
| [`automation-infra/`](automation-infra/README.md) | Terraform for a Cloud Run job that runs the scenario-runner on a schedule or on demand, using the latest commit of a branch. |
| [`dependency-repos/substrate/`](dependency-repos/substrate/README.md) | The substrate source, as a git submodule. The scenario-runner builds and installs substrate from it. |
