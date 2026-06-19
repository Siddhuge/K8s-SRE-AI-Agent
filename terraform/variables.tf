variable "location" {
  description = "Azure region"
  type        = string
  default     = "eastus"
}

variable "prefix" {
  description = "Name prefix for all resources"
  type        = string
  default     = "k8ssre"
}

variable "kubernetes_version" {
  description = "AKS version; null = AKS default for the region"
  type        = string
  default     = null
}

variable "node_count" {
  description = "Node count (1 is enough for the validation; keep it small to save cost)"
  type        = number
  default     = 1
}

variable "node_vm_size" {
  description = "Node VM size (cheap burstable default)"
  type        = string
  default     = "Standard_B2s"
}

variable "agent_namespace" {
  description = "Namespace the agent runs in (must match the federated-credential subject)"
  type        = string
  default     = "sre-system"
}

variable "agent_sa_name" {
  description = "Agent ServiceAccount name (must match the Helm chart's SA)"
  type        = string
  default     = "k8s-sre-agent"
}

variable "create_acr" {
  description = "Also create an ACR + attach it (a registry to push the agent image to)"
  type        = bool
  default     = true
}

variable "tags" {
  type    = map(string)
  default = { app = "k8s-sre-agent", purpose = "workload-identity-validation" }
}
