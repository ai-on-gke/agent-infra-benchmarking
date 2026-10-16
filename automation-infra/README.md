# Automation

A Cloud Run job that runs the scenario-runner (`benchmarking/tools/run`), on a
schedule or by hand. Each run uses the latest commit of one branch, which
[`.github/workflows/upload-source.yml`](../.github/workflows/upload-source.yml)
uploads to a bucket on every push.

A deployment is a folder under [`deployments/`](deployments) that calls the
[`terraform/modules/cloud-run`](terraform/modules/cloud-run) module. The module
deploys the job and builds its image. It also creates the network the runs'
clusters go into, which the job joins: their control planes have an internal
endpoint only, and their nodes reach out through its Cloud NAT. See
[`deployments/substrate-bench-auto`](deployments/substrate-bench-auto) for an
example.

## Prerequisites

- Terraform 1.5+
- gcloud, git, jq
- gh, logged in with permission to set this repo's Actions variables and secrets
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
   minutes. The image is rebuilt whenever the commit or the image's files change.
   The build is started from this machine, with `gcloud builds submit`, as the
   module's build account; building it from a Cloud Build trigger instead would
   need the repo connected to Cloud Build, which an organization admin has to
   set up:

   ```bash
   terraform -chdir="automation-infra/deployments/$DEPLOYMENT" apply
   ```

4. Let GitHub Actions upload the source without a key: this sets the repo
   variables `GCP_WORKLOAD_IDENTITY_PROVIDER_<DEPLOYMENT>` and
   `GCP_SERVICE_ACCOUNT_<DEPLOYMENT>`, which the workflow authenticates with
   through Workload Identity Federation, and deletes any key of that account
   and the old `GCP_CREDENTIALS_<DEPLOYMENT>` secret:

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
execution only; a `--scenario` or `--all` replaces the cases the job's `args`
name:

```bash
automation-infra/tools/trigger --deployment="$DEPLOYMENT" --scenario=glutton
```
