# Automation

Runs the cases in [`tests.yaml`](tests.yaml) as a Cloud Run job, on a schedule
or by hand. [`infra/cloud-run`](infra/cloud-run) deploys the Cloud Run job and
builds its image; the Cloud Run job runs
[`runner/run_tests.py`](runner/run_tests.py), which creates
[`runner/infra`](runner/infra) for each execution and destroys it afterwards.

## Prerequisites

- **Terraform 1.5+**. The lock files pin the providers.
- **`gcloud`**.
- **`git`**. The Cloud Run job's image is tagged with the repo's commit.
- A GCP project with billing, and an account with `Owner` on it. `Editor` is
  not enough, as it cannot set IAM.

### Running `run_tests.py` locally

- **Python 3.11+ with PyYAML**.
