output "cluster_name" {
  description = "Name of the fake cluster."
  value       = terraform_data.cluster.output
}

output "sandboxed_nodes" {
  description = "Names of the fake sandboxed nodes."
  value       = terraform_data.sandboxed_node[*].output
}
