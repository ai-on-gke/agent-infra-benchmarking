# Automation

A Cloud Run job, run on a schedule or by hand. A deployment is a folder under
[`deployments/`](deployments) that calls the
[`terraform/modules/cloud-run`](terraform/modules/cloud-run) module, which
deploys the Cloud Run job and builds its image.

For now the image is a placeholder,
[`image/Dockerfile`](terraform/modules/cloud-run/image/Dockerfile), that exits
successfully and does nothing else.

## Prerequisites

- **Terraform 1.5+**. The lock files pin the providers.
- **`gcloud`**.
- **`git`**. The Cloud Run job's image is tagged with the repo's commit.
- A GCP project with billing, and an account with `Owner` on it. `Editor` is
  not enough, as it cannot set IAM.

## Preparing

1. Log in to `gcloud`:

   ```bash
   gcloud auth login
   ```

2. Log in once more for Terraform, which reads the application-default
   credentials:

   ```bash
   gcloud auth application-default login
   ```

3. Set the values the steps below use: the GCP project and the deployment's
   name, which you fill in, the region, and the bucket that will hold the
   deployment's Terraform state, named after it. The deployment's name is also
   the name of the resources it creates: 6-30 lowercase letters, digits and
   hyphens, starting with a letter.

   ```bash
   PROJECT_ID='<YOUR_PROJECT>' \
   DEPLOYMENT='<DEPLOYMENT_NAME>' \
   REGION=us-central1 \
   STATE_BUCKET="$DEPLOYMENT-tfstate"
   ```

4. Enable the APIs Terraform needs before it can enable the rest:

   ```bash
   gcloud services enable serviceusage.googleapis.com iam.googleapis.com --project="$PROJECT_ID"
   ```

5. Create the bucket for the deployment's state, with versioning to recover
   an overwritten state:

   ```bash
   gcloud storage buckets create "gs://$STATE_BUCKET" --project="$PROJECT_ID" --location="$REGION" --uniform-bucket-level-access
   ```

   ```bash
   gcloud storage buckets update "gs://$STATE_BUCKET" --versioning
   ```

## Creating a deployment

In the same shell, with the variables from [Preparing](#preparing) still set:

1. Create the deployment's folder, with its state in the bucket under its own
   prefix, and the state of the infra each test run creates under
   `state_bucket_prefix`. Any other variable of the module, see its
   [`variables.tf`](terraform/modules/cloud-run/variables.tf), goes in the
   `module` block, e.g. `schedule = "0 3 * * *"` to run every day at 3am UTC:

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

     state_bucket        = local.state_bucket
     state_bucket_prefix = "tests"
   }

   output "cloud_run" {
     value = module.cloud_run
   }
   EOF

   printf '.terraform/\n' > "automation-infra/deployments/$DEPLOYMENT/.gitignore"
   ```

2. Initialize it, which also writes the lock file that pins the providers:

   ```bash
   terraform -chdir="automation-infra/deployments/$DEPLOYMENT" init
   ```

3. Apply. This also builds the job's image on Cloud Build, which takes a few
   minutes, and again whenever the commit or the image's files change:

   ```bash
   terraform -chdir="automation-infra/deployments/$DEPLOYMENT" apply
   ```

4. Commit the folder: `main.tf`, `.gitignore` and `.terraform.lock.hcl`.

   ```bash
   git add "automation-infra/deployments/$DEPLOYMENT"
   ```

5. Run it once, now, and wait for it to finish:

   ```bash
   gcloud run jobs execute "$DEPLOYMENT" --project="$PROJECT_ID" --region="$REGION" --wait
   ```

   Or with [`scripts/trigger.sh`](scripts/trigger.sh), which starts it through
   the Cloud Run Admin API, as an external caller would, and prints its log as
   it comes in. It also needs `jq`:

   ```bash
   automation-infra/scripts/trigger.sh "$DEPLOYMENT"
   ```
