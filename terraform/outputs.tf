output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.this.name
}

output "oidc_issuer_url" {
  value = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "agent_client_id" {
  description = "Put this in clusters.yaml auth.clientId AND the SA annotation"
  value       = azurerm_user_assigned_identity.agent.client_id
}

output "tenant_id" {
  value = azurerm_user_assigned_identity.agent.tenant_id
}

output "aks_api_server" {
  description = "clusters.yaml auth.server"
  value       = "https://${azurerm_kubernetes_cluster.this.fqdn}:443"
}

output "acr_login_server" {
  value = var.create_acr ? azurerm_container_registry.this[0].login_server : null
}

output "get_credentials_cmd" {
  description = "Admin kubeconfig (to deploy the agent + run the validation Job)"
  value       = "az aks get-credentials -g ${azurerm_resource_group.this.name} -n ${azurerm_kubernetes_cluster.this.name} --admin"
}

# Ready-to-paste cluster registry entry for the agent (config/clusters.yaml).
output "clusters_yaml_snippet" {
  value = <<-EOT
    defaultCluster: aks-wi
    clusters:
      - name: aks-wi
        tenant: payments
        provider: aks
        auth:
          mode: azure_workload
          server: https://${azurerm_kubernetes_cluster.this.fqdn}:443
          clientId: ${azurerm_user_assigned_identity.agent.client_id}
          tenantId: ${azurerm_user_assigned_identity.agent.tenant_id}
        allowedNamespaces: ["default", "payments", "sre-system"]
  EOT
}
