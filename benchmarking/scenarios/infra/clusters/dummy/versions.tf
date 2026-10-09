terraform {
  required_version = ">= 1.5"

  # No providers: terraform_data is built into Terraform, so this config needs
  # no download and no GCP credentials. No backend either; the scenario-runner
  # writes one, as for common.
}
