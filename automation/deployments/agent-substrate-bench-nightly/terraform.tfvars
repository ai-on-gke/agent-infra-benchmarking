project_id = "akvelon-gke-aieco"
region     = "us-central1"

default_resource_name = "agent-substrate-bench-nightly"

env = {}

cpu          = "2"
memory       = "4Gi"
task_timeout = "86400s"

# 3am UTC every day.
schedule  = "0 3 * * *"
time_zone = "Etc/UTC"
