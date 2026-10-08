region = "us-central1"

# Lets terraform destroy delete the bucket, and with it every run's reports
# under reports/. Download any worth keeping first, or set this to false.
bucket_force_destroy = true

cluster = {
  # The control plane's version and every node pool's; bump it deliberately.
  # GKE may still upgrade the control plane past it. The pools below stay put
  # until the minor version's end of support.
  kubernetes_version = "1.35.8-gke.1225000"
  release_channel    = "STABLE"

  # Without these, substrate's podcertificate-controller never creates the
  # ClusterTrustBundles and hack/install-ate.sh waits for them forever.
  k8s_beta_apis = [
    "certificates.k8s.io/v1beta1/clustertrustbundles",
    "certificates.k8s.io/v1beta1/podcertificaterequests",
  ]

  node_pools = {
    default = {
      machine_type   = "c2-standard-16"
      node_count     = 1
      node_locations = ["us-central1-a"]
      auto_upgrade   = false
      min_node_count = 1
      max_node_count = 1
    }
    sandboxed = {
      machine_type   = "c2-standard-16"
      node_locations = ["us-central1-a"]
      # The pool's size comes from sandboxed_node_count,
      # sandboxed_min_node_count and sandboxed_max_node_count below.
      # ate.dev/substrate-version is not set here: Terraform reads it off the
      # substrate submodule and adds it to every pool. See main.tf.
      labels = {
        "benchmarking-worker" = "true",
      }
      # worker-pool.yaml.tmpl's WorkerPool already tolerates this taint; without
      # it actually applied to the nodes, nothing stops substrate's own
      # control-plane pods (and anything else) from also landing here
      # alongside the workers, which is how a 100-worker run blows past GKE's
      # 110-pods-per-node cap on this single pinned node.
      taints = [
        {
          key    = "ate.dev/sandboxClass"
          value  = "gvisor"
          effect = "NO_SCHEDULE"
        }
      ]
      # An upgrade drains the workers, crashing the actors awake on them.
      auto_upgrade = false
    }
  }
}

# Size of cluster.node_pools.sandboxed, kept out of it so a run can set it
# with -var. All three default to 1.
sandboxed_node_count     = 1
sandboxed_min_node_count = 1
sandboxed_max_node_count = 1
