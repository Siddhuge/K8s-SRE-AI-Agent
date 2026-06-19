data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "this" {
  name     = "${var.prefix}-rg"
  location = var.location
  tags     = var.tags
}

# Optional registry for the agent image (so AKS can pull it).
resource "azurerm_container_registry" "this" {
  count               = var.create_acr ? 1 : 0
  name                = "${var.prefix}acr${substr(md5(azurerm_resource_group.this.id), 0, 6)}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "Basic"
  tags                = var.tags
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = "${var.prefix}-aks"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = "${var.prefix}-aks"
  kubernetes_version  = var.kubernetes_version

  # The two features the azure_workload auth path depends on:
  oidc_issuer_enabled       = true # publishes the OIDC issuer the federated cred trusts
  workload_identity_enabled = true # injects the projected SA token + AZURE_* env into pods

  default_node_pool {
    name       = "system"
    node_count = var.node_count
    vm_size    = var.node_vm_size
  }

  identity {
    type = "SystemAssigned"
  }

  # Entra-integrated cluster with Azure RBAC for Kubernetes — required for the agent's
  # AAD token to authorize against the kube API. Read-only is granted below via a role
  # assignment. (local accounts stay enabled so you can `get-credentials --admin` to deploy.)
  azure_active_directory_role_based_access_control {
    tenant_id          = data.azurerm_client_config.current.tenant_id
    azure_rbac_enabled = true
  }

  tags = var.tags
}

# Let the cluster's kubelet pull from the ACR.
resource "azurerm_role_assignment" "acr_pull" {
  count                            = var.create_acr ? 1 : 0
  scope                            = azurerm_container_registry.this[0].id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}

# The agent's workload identity (federated to the AKS OIDC issuer).
resource "azurerm_user_assigned_identity" "agent" {
  name                = "${var.prefix}-agent"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "agent" {
  name                = "agent-sa"
  resource_group_name = azurerm_resource_group.this.name
  parent_id           = azurerm_user_assigned_identity.agent.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject             = "system:serviceaccount:${var.agent_namespace}:${var.agent_sa_name}"
}

# Read-only data-plane access for the agent identity (Azure RBAC). This is what makes
# the validation meaningful: the federated identity can LIST but not delete/exec/etc.
resource "azurerm_role_assignment" "agent_reader" {
  scope                = azurerm_kubernetes_cluster.this.id
  role_definition_name = "Azure Kubernetes Service RBAC Reader"
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}
