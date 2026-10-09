region = "us-central1"

# Lets terraform destroy delete the bucket, and with it every run's reports
# under reports/. Download any worth keeping first, or set this to false.
bucket_force_destroy = true

sandboxed_machine_type = "c2-standard-16"

# mock-llm alone, which mock-llm.yaml.tmpl is pinned to, so the LLM the
# openclaw actors call shares no node with substrate's control plane. Its 10
# replicas ask for 500m CPU each.
additional_node_pools = [{
  name         = "mock-llm"
  machine_type = "e2-standard-8"
  labels       = { "benchmarking-mock-llm" = "true" }
  taints = [{
    key    = "benchmarking-mock-llm"
    value  = "true"
    effect = "NO_SCHEDULE"
  }]
}]
