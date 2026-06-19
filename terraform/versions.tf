terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.116"
    }
  }
}

provider "azurerm" {
  features {}
  # Auth comes from `az login` (or ARM_* env vars / a service principal in CI).
  # subscription_id can be set here or via ARM_SUBSCRIPTION_ID.
}
