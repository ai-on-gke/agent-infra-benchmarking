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

## Preparing

1. Set the values the steps below use: the GCP project, the region, the
   bucket that will hold `infra/cloud-run`'s Terraform state, and the name of
   the resources `infra/cloud-run` creates:

   ```bash
   export PROJECT_ID='<YOUR_PROJECT>' REGION=us-central1 STATE_BUCKET=agent-substrate-auto-benchmarking-state DEFAULT_RESOURCE_NAME=bench-automation
   ```

2. Log in to `gcloud`:

   ```bash
   gcloud auth login
   ```

3. Log in once more for Terraform, which reads the application-default
   credentials:

   ```bash
   gcloud auth application-default login
   ```

4. Enable the APIs Terraform needs before it can enable the rest:

   ```bash
   gcloud services enable serviceusage.googleapis.com iam.googleapis.com --project="$PROJECT_ID"
   ```

5. Create the bucket for `infra/cloud-run`'s state, with versioning to recover
   an overwritten state:

   ```bash
   gcloud storage buckets create "gs://$STATE_BUCKET" --project="$PROJECT_ID" --location="$REGION" --uniform-bucket-level-access
   ```

   ```bash
   gcloud storage buckets update "gs://$STATE_BUCKET" --versioning
   ```

6. Let Cloud Build push the image. It builds as the Compute Engine default
   service account, which can unless an org policy took its default roles
   away. In that case:

   ```bash
   export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
   ```

   ```bash
   gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role=roles/cloudbuild.builds.builder --condition=None
   ```

## Deploying

With the variables from [Preparing](#preparing) still exported:

1. Initialize `infra/cloud-run` with its state in the bucket:

   ```bash
   terraform -chdir=automation/infra/cloud-run init -backend-config=bucket="$STATE_BUCKET" -backend-config=prefix=automation/cloud-run
   ```

2. Pass its variables through the environment, including the arguments the
   Cloud Run job gives `run_tests.py`. The runner state bucket is the one
   `infra/cloud-run` creates, named `<project>-<default_resource_name>`:

   ```bash
   export TF_VAR_project_id="$PROJECT_ID" TF_VAR_region="$REGION" TF_VAR_default_resource_name="$DEFAULT_RESOURCE_NAME"
   ```

   ```bash
   export TF_VAR_args="[\"--project-id=$PROJECT_ID\", \"--region=$REGION\", \"--state-bucket=$PROJECT_ID-$DEFAULT_RESOURCE_NAME\"]"
   ```

   To run it on a schedule, also set a cron schedule, e.g. every day at 3am
   UTC. Without one it only runs when started by hand:

   ```bash
   export TF_VAR_schedule='0 3 * * *'
   ```

3. Apply. This also builds the runner image on Cloud Build, which takes a few
   minutes, and again whenever the commit or the image's files change:

   ```bash
   terraform -chdir=automation/infra/cloud-run apply
   ```

4. Run it once, now, and wait for it to finish:

   ```bash
   gcloud run jobs execute "$DEFAULT_RESOURCE_NAME" --project="$PROJECT_ID" --region="$REGION" --wait
   ```

## Cleanup

If you granted Cloud Build's service account a role while preparing, revoke it,
with the variables from [Preparing](#preparing) still exported:

```bash
gcloud projects remove-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role=roles/cloudbuild.builds.builder --condition=None
```
