terraform {
  required_version = ">= 1.5"

  # The bucket and prefix are given to terraform init:
  #   terraform init -backend-config=bucket=BUCKET -backend-config=prefix=automation/cloud-run
  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.33.0, < 8.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
