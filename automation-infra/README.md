# Automation

A Cloud Run job that runs the scenario-runner (`benchmarking/tools/run`), on a
schedule or by hand. Each run uses the latest commit of one branch, which
[`.github/workflows/upload-source.yml`](../.github/workflows/upload-source.yml)
uploads to a bucket on every push.

A deployment is a folder under [`deployments/`](deployments) that calls the
[`terraform/modules/cloud-run`](terraform/modules/cloud-run) module. The module
deploys the job and builds its image. See
[`deployments/substrate-bench-auto`](deployments/substrate-bench-auto) for an
example.

## Prerequisites

- Terraform 1.5+
- gcloud, git, jq
- gh, logged in with permission to set this repo's Actions secrets
- A GCP project with billing, and an account with `Owner` on it. `Editor` is
  not enough, because it cannot set IAM.

## Preparing

1. Log in to gcloud:

   ```bash
   gcloud auth login
   ```

2. Log in for Application Default Credentials, which Terraform uses:

   ```bash
   gcloud auth application-default login
   ```

3. Set the variables the steps below use. Fill in the project, the deployment's
   name and the branch the job runs. The deployment's name is also the name of
   the resources it creates: 6-30 lowercase letters, digits and hyphens,
   starting with a letter.

   ```bash
   PROJECT_ID='<YOUR_PROJECT>' \
   DEPLOYMENT='<DEPLOYMENT_NAME>' \
   SOURCE_BRANCH='<BRANCH>' \
   REGION=us-central1 \
   STATE_BUCKET="$DEPLOYMENT-tfstate"
   ```

4. Enable the APIs Terraform needs before it can enable the rest:

   ```bash
   gcloud services enable serviceusage.googleapis.com iam.googleapis.com --project="$PROJECT_ID"
   ```

5. Create the bucket that holds the deployment's Terraform state and the
   uploaded source:

   ```bash
   gcloud storage buckets create "gs://$STATE_BUCKET" --project="$PROJECT_ID" --location="$REGION" --uniform-bucket-level-access
   ```

6. Turn on versioning for the bucket, so an overwritten state can be recovered:

   ```bash
   gcloud storage buckets update "gs://$STATE_BUCKET" --versioning
   ```

## Creating a deployment

Run these from the repo root, in the same shell as [Preparing](#preparing):

1. Create the deployment's folder. To set other module variables, see its
   [`variables.tf`](terraform/modules/cloud-run/variables.tf) and add them to the
   `module` block. For example, `schedule = "0 3 * * *"` runs the job every day
   at 3am UTC.

   ```bash
   mkdir -p "automation-infra/deployments/$DEPLOYMENT"

   cat > "automation-infra/deployments/$DEPLOYMENT/main.tf" <<EOF
   terraform {
     required_version = ">= 1.5"

     backend "gcs" {
       bucket = "$STATE_BUCKET"
       prefix = "automation/cloud-run/$DEPLOYMENT"
     }
   }

   locals {
     # The backend's bucket, which the backend block cannot take from here. Also
     # read by .github/workflows/upload-source.yml.
     state_bucket = "$STATE_BUCKET"
   }

   module "cloud_run" {
     source = "../../terraform/modules/cloud-run"

     project_id            = "$PROJECT_ID"
     region                = "$REGION"
     default_resource_name = "$DEPLOYMENT"

     state_bucket          = local.state_bucket
     state_bucket_prefix   = "terraform"
     github_actions_bucket = local.state_bucket
     source_branch         = "$SOURCE_BRANCH"
   }

   output "cloud_run" {
     value = module.cloud_run
   }
   EOF

   printf '.terraform/\n' > "automation-infra/deployments/$DEPLOYMENT/.gitignore"
   ```

2. Initialize the deployment. This writes the lock file that pins the providers:

   ```bash
   terraform -chdir="automation-infra/deployments/$DEPLOYMENT" init
   ```

3. Apply. This also builds the job's image on Cloud Build, which takes a few
   minutes. The image is rebuilt whenever the commit or the image's files change:

   ```bash
   terraform -chdir="automation-infra/deployments/$DEPLOYMENT" apply
   ```

4. Store the key that GitHub Actions uploads the source with as the repo secret
   `GCP_CREDENTIALS_<DEPLOYMENT>`. Running this again rotates the key:

   ```bash
   automation-infra/tools/get-gh-actions-gcs-config --deployment="$DEPLOYMENT" --set
   ```

5. Add the deployment to the `deployment` matrix of both jobs in
   `upload-source.yml`:

   ```bash
   sed -i "s/deployment: \[\(.*\)\]/deployment: [\1, $DEPLOYMENT]/" .github/workflows/upload-source.yml
   ```

6. Commit the deployment's folder (`main.tf`, `.gitignore` and
   `.terraform.lock.hcl`) and the workflow, then push to `$SOURCE_BRANCH`. The
   push uploads the source that the job runs. Wait for the Upload source
   workflow to finish.

7. Run the `dummy` case to check the deployment. It creates no cluster:

   ```bash
   automation-infra/tools/trigger-dummy --deployment="$DEPLOYMENT"
   ```

## Running the job

[`tools/trigger`](tools/trigger) starts one execution and prints its log as it
comes in. Any other argument is passed to the scenario-runner, for that
execution only:

```bash
automation-infra/tools/trigger --deployment="$DEPLOYMENT" --scenario=glutton
```

## Keeping your own runs' reports in BigQuery

A run from your machine keeps its reports only in `reports/` once its cluster
is gone. To query them in BigQuery, as the scheduled runs' are, create a
deployment of only the [`terraform/modules/reports`](terraform/modules/reports)
module: a bucket and a dataset of views over it, with no Cloud Run job. Then
point your runs at it by its name:

```bash
./benchmarking/tools/run --report-deployment="$DEPLOYMENT" --scenario glutton
```

### Creating the reports deployment

1. Do steps 1-6 of [Preparing](#preparing). `SOURCE_BRANCH` is not used. The
   deployment's name also names its bucket, `$DEPLOYMENT-reports`, which must
   not be taken in any project.

2. Create the deployment's folder. If your runs' clusters are in another
   project than `$PROJECT_ID`, add `writer_project_ids = ["<that project>"]` to
   the `module` block, so their uploads are allowed:

   ```bash
   mkdir -p "automation-infra/deployments/$DEPLOYMENT"

   cat > "automation-infra/deployments/$DEPLOYMENT/main.tf" <<EOF
   terraform {
     required_version = ">= 1.5"

     backend "gcs" {
       bucket = "$STATE_BUCKET"
       prefix = "automation/reports/$DEPLOYMENT"
     }
   }

   module "reports" {
     source = "../../terraform/modules/reports"

     project_id = "$PROJECT_ID"
     region     = "$REGION"
     name       = "$DEPLOYMENT"
   }

   output "reports" {
     value = module.reports
   }
   EOF

   printf '.terraform/\n' > "automation-infra/deployments/$DEPLOYMENT/.gitignore"
   ```

3. Initialize and apply it:

   ```bash
   terraform -chdir="automation-infra/deployments/$DEPLOYMENT" init
   terraform -chdir="automation-infra/deployments/$DEPLOYMENT" apply
   ```

Commit the folder only if others are to use the deployment too.

### Running into it

`--report-deployment` sends a run's reports to
`gs://$DEPLOYMENT-reports/data/<run-id>/<record>/` rather than to the
cluster's own bucket. It works with any other option, `--dev-infra` included,
and a local copy still lands in `reports/<run-id>/`:

```bash
./benchmarking/tools/run --report-deployment="$DEPLOYMENT" --scenario glutton
```

The run uploads the Jobs' logs with your own credentials, so your account
needs write access to the bucket; `Owner` of the project has it.

### Querying the reports

The dataset is the deployment's name with underscores for hyphens. The views
are the same as [substrate-bench-auto's](deployments/substrate-bench-auto/README.md#querying-the-reports),
each a row per run and record. A view fails until a run has written the file it
reads, so a sandbox-benchmark case's views work only after one has run.

Each glutton or openclaw run's steady-state latency:

```bash
bq query --project_id="$PROJECT_ID" --use_legacy_sql=false \
  "SELECT run_id, test_case, requests_per_sec, p50_ms, p95_ms FROM ${DEPLOYMENT//-/_}.locust_stats_steady WHERE name = 'CycleTime' ORDER BY 1, 2"
```

Each sandbox-benchmark run's metrics, per provider:

```bash
bq query --project_id="$PROJECT_ID" --use_legacy_sql=false \
  "SELECT run_id, test_case, test, provider, metric, mean_ms, stddev_ms FROM ${DEPLOYMENT//-/_}.sandbox_benchmark_comparison ORDER BY 1, 2, 3, 5, 4"
```

### Removing it

The bucket is not deleted while it holds reports. To drop them and everything
else the deployment created:

```bash
gcloud storage rm --recursive "gs://$DEPLOYMENT-reports/**"
terraform -chdir="automation-infra/deployments/$DEPLOYMENT" destroy
```
