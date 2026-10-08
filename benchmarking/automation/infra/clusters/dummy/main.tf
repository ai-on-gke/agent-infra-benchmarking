# A cluster made of terraform_data resources, which only live in the state:
# applying and destroying it exercises a case's Terraform path in seconds, for
# nothing.

resource "terraform_data" "cluster" {
  input = var.default_resource_name

  lifecycle {
    precondition {
      condition     = var.sandboxed_min_node_count <= var.sandboxed_node_count && var.sandboxed_node_count <= var.sandboxed_max_node_count
      error_message = "sandboxed_node_count must be between sandboxed_min_node_count and sandboxed_max_node_count."
    }
  }
}

resource "terraform_data" "sandboxed_node" {
  count = var.sandboxed_node_count

  input = "${terraform_data.cluster.output}-sandboxed-${count.index}"
}
