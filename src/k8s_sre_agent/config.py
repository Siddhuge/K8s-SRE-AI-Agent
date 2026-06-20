"""Settings + cluster registry loading.

Secrets are resolved from environment variables (12-factor). In production these
come from the platform secret store (Azure Key Vault / AWS Secrets Manager) and are
projected as env vars or files by the deployment — never baked into images.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    clusters_config: str = "config/clusters.yaml"
    default_cluster: str = ""

    # Notification guardrails — outward-facing tools are opt-in.
    allow_notifications: bool = False
    slack_allowed_channels: str = ""
    teams_allowed_channels: str = ""

    # Default observability backends (per-cluster overrides take precedence).
    prometheus_url: str = ""
    loki_url: str = ""
    grafana_url: str = ""
    grafana_token: str = ""

    argocd_server: str = ""
    argocd_token: str = ""
    gitlab_url: str = ""
    gitlab_token: str = ""
    github_token: str = ""

    jira_url: str = ""
    jira_email: str = ""
    jira_token: str = ""
    servicenow_url: str = ""
    servicenow_user: str = ""
    servicenow_password: str = ""
    slack_bot_token: str = ""
    teams_webhook_url: str = ""

    rag_enabled: bool = False
    pgvector_dsn: str = ""
    embedding_model: str = "voyage-3"
    embedding_dim: int = 1024

    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_required_groups: str = ""

    # Rate limiting (per authenticated principal).
    ratelimit_rate: float = 5.0
    ratelimit_burst: int = 30
    # Empty → in-memory per-replica limiter. Set (e.g. redis://host:6379/0) for a strict
    # GLOBAL limit shared across all gateway replicas.
    ratelimit_redis_url: str = ""

    def allowed_slack_channels(self) -> set[str]:
        return {c.strip() for c in self.slack_allowed_channels.split(",") if c.strip()}

    def validate_for(self, transport: str) -> list[str]:
        """Fail-fast config validation. Returns a list of fatal problems (empty = ok)."""
        problems: list[str] = []
        if transport == "http" and not self.oidc_issuer:
            problems.append("OIDC_ISSUER must be set for the HTTP transport (refusing unauthenticated access)")
        if self.allow_notifications and not (self.slack_bot_token or self.teams_webhook_url):
            problems.append("ALLOW_NOTIFICATIONS=true but neither SLACK_BOT_TOKEN nor TEAMS_WEBHOOK_URL is set")
        if self.rag_enabled and not self.pgvector_dsn:
            problems.append("RAG_ENABLED=true but PGVECTOR_DSN is empty")
        return problems


class ClusterAuth(BaseModel):
    mode: Literal[
        "kubeconfig", "in_cluster", "azure_workload", "aws_eks", "oidc_exec"
    ]
    server: str = ""
    kubeconfig: str = ""
    context: str = ""
    # azure_workload
    client_id: str = Field("", alias="clientId")
    tenant_id: str = Field("", alias="tenantId")
    # aws_eks
    cluster_name: str = Field("", alias="clusterName")
    role_arn: str = Field("", alias="roleArn")

    model_config = SettingsConfigDict(populate_by_name=True)


class ClusterObservability(BaseModel):
    prometheus: str = ""
    loki: str = ""
    grafana: str = ""


class ClusterGitOps(BaseModel):
    argocd: str = ""
    project: str = ""


class ClusterConfig(BaseModel):
    name: str
    tenant: str = "default"
    provider: str = "kubernetes"
    region: str = ""
    auth: ClusterAuth
    observability: ClusterObservability = ClusterObservability()
    gitops: ClusterGitOps = ClusterGitOps()
    allowed_namespaces: list[str] = Field(default_factory=lambda: ["*"], alias="allowedNamespaces")

    model_config = SettingsConfigDict(populate_by_name=True)

    def namespace_allowed(self, namespace: str) -> bool:
        return "*" in self.allowed_namespaces or namespace in self.allowed_namespaces


class ClusterRegistry(BaseModel):
    default_cluster: str = Field("", alias="defaultCluster")
    clusters: list[ClusterConfig]

    model_config = SettingsConfigDict(populate_by_name=True)

    def get(self, name: str | None) -> ClusterConfig:
        target = name or self.default_cluster
        for c in self.clusters:
            if c.name == target:
                return c
        raise KeyError(
            f"Unknown cluster {target!r}. Registered: {[c.name for c in self.clusters]}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_registry() -> ClusterRegistry:
    settings = get_settings()
    path = Path(settings.clusters_config)
    if not path.exists():
        # Fall back to a single in-cluster entry so the agent still runs with
        # nothing but a mounted ServiceAccount (the simplest POC deployment).
        return ClusterRegistry(
            defaultCluster="in-cluster",
            clusters=[ClusterConfig(name="in-cluster", auth=ClusterAuth(mode="in_cluster"))],
        )
    data = yaml.safe_load(path.read_text())
    registry = ClusterRegistry.model_validate(data)
    if settings.default_cluster:
        registry.default_cluster = settings.default_cluster
    return registry
