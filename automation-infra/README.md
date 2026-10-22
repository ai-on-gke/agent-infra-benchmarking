# Automation

A Cloud Run job that runs the scenario-runner (`benchmarking/tools/run`), on a
schedule or by hand. Each run uses the image of the latest commit of one
branch, which
[`.github/workflows/publish.yml`](../.github/workflows/publish.yml)
builds from [`image/Dockerfile`](image/Dockerfile) on every push.

A deployment is a folder under [`deployments/`](deployments) that calls the
[`terraform/modules/cloud-run`](terraform/modules/cloud-run) module. The module
deploys the job and the repository its image goes to. It also creates the
network the runs' clusters go into, which the job joins: their control planes
have an internal endpoint only, and their nodes reach out through its Cloud
NAT. See
[`deployments/substrate-bench-auto`](deployments/substrate-bench-auto) for an
example.

## Prerequisites

- Terraform 1.5+
- gcloud, git, jq
- gh, logged in with permission to set this repo's Actions variables
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

5. Create the bucket that holds the deployment's Terraform state:

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

     required_providers {
       github = {
         source = "integrations/github"
       }
     }

     backend "gcs" {
       bucket = "$STATE_BUCKET"
       prefix = "automation/cloud-run/$DEPLOYMENT"
     }
   }

   locals {
     # The backend's bucket, which the backend block cannot take from here.
     state_bucket = "$STATE_BUCKET"
   }

   variable "create_jobs" {
     description = "False for the first apply, before the workflow has pushed the runner image."
     type        = bool
     default     = true
   }

   provider "github" {
     owner = "ai-on-gke"
   }

   module "cloud_run" {
     source = "../../terraform/modules/cloud-run"

     project_id            = "$PROJECT_ID"
     region                = "$REGION"
     default_resource_name = "$DEPLOYMENT"
     create_jobs           = var.create_jobs

     state_bucket        = local.state_bucket
     state_bucket_prefix = "terraform"
     source_branch       = "$SOURCE_BRANCH"
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

3. Apply everything but the jobs, which need the image the workflow can only
   push once this apply has let it. This also sets the repo variables
   `GCP_WORKLOAD_IDENTITY_PROVIDER_<DEPLOYMENT>` and
   `GCP_SERVICE_ACCOUNT_<DEPLOYMENT>`, which the workflows authenticate with
   through Workload Identity Federation, as your `gh` login:

   ```bash
   terraform -chdir="automation-infra/deployments/$DEPLOYMENT" apply -var=create_jobs=false
   ```

4. Add the deployment to the `deployment` matrix of every job in
   `publish.yml`:

   ```bash
   sed -i "s/deployment: \[\(.*\)\]/deployment: [\1, $DEPLOYMENT]/" .github/workflows/publish.yml
   ```

5. Commit the deployment's folder (`main.tf`, `.gitignore` and
   `.terraform.lock.hcl`) and the workflow, then push to `$SOURCE_BRANCH`. A
   push to `main` builds the job's image. For any other branch, run the Publish
   workflow on it by hand, after this push and each later one:

   ```bash
   gh workflow run Publish --ref "$SOURCE_BRANCH"
   ```

   Wait for the Publish workflow to finish.

6. Apply again, which creates the jobs, running the image the workflow pushed:

   ```bash
   terraform -chdir="automation-infra/deployments/$DEPLOYMENT" apply
   ```

7. Run the `dummy` case to check the deployment. It creates no cluster:

   ```bash
   automation-infra/tools/trigger-dummy --deployment="$DEPLOYMENT"
   ```

## Running the job

[`tools/trigger`](tools/trigger) starts one execution and prints its log as it
comes in. Any other argument is passed to the scenario-runner, for that
execution only; a `--scenario` or `--all` replaces the cases the job's `args`
name:

```bash
automation-infra/tools/trigger --deployment="$DEPLOYMENT" --scenario=glutton-gvisor
```

## Keeping your own runs' reports in BigQuery

A run from your machine keeps its reports only in `reports/` once its cluster
is gone. To query them in BigQuery, as the scheduled runs' are, apply
[`dev/reports`](dev/reports): a bucket and a dataset of views over it, with no
Cloud Run job. Then point your runs at its folder:

```bash
./benchmarking/tools/run --report-deployment=automation-infra/dev/reports --scenario glutton-gvisor
```

### Creating the reports deployment

Its Terraform state stays in `dev/reports/terraform.tfstate`, gitignored, so
each checkout has one. Keep the file: destroying the deployment needs it.

1. Log in to gcloud and for Application Default Credentials, as in steps 1-2 of
   [Preparing](#preparing).

2. Set the project and the deployment's name, which the steps below use. The
   name also names the bucket, `$DEPLOYMENT-reports`, which must not be taken
   in any project: 6-30 lowercase letters, digits and hyphens, starting with a
   letter.

   ```bash
   PROJECT_ID='<YOUR_PROJECT>' DEPLOYMENT='<DEPLOYMENT_NAME>'
   ```

3. Write its `terraform.tfvars`. If your runs' clusters are in another project
   than `$PROJECT_ID`, also add `writer_project_ids = ["<that project>"]`, so
   their uploads are allowed:

   ```bash
   printf 'project_id = "%s"\nname       = "%s"\n' "$PROJECT_ID" "$DEPLOYMENT" > automation-infra/dev/reports/terraform.tfvars
   ```

4. Initialize it:

   ```bash
   terraform -chdir=automation-infra/dev/reports init
   ```

5. Apply it. It creates the bucket, the dataset, the bucket's grant to the
   clusters and the dataset's tables and views:

   ```bash
   terraform -chdir=automation-infra/dev/reports apply
   ```

### Running into it

`--report-deployment` reads the bucket and the prefix from the folder's
`reports` output, as `terraform output` shows them, and sends a run's reports
to `gs://$DEPLOYMENT-reports/data/<run-id>/<record>/` rather than to the
cluster's own bucket. It works with any other option, `--dev-infra` included,
and a local copy still lands in `reports/<run-id>/`:

```bash
./benchmarking/tools/run --report-deployment=automation-infra/dev/reports --scenario glutton-gvisor
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
terraform -chdir=automation-infra/dev/reports destroy
```
