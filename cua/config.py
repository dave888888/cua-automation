"""Loading config layers and resolving secrets.

Secrets are never in config, artifacts or logs: a tenant maps a secret *ref*
(``corelink.password``) to an environment variable name. In production this is
the seam to a vault (per-tenant credentials, short-lived, checked out per run).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .schema.config import AppProfile, Policy, TenantConfig

ROOT = Path(os.environ.get("CUA_HOME", Path(__file__).resolve().parent.parent))
CONFIG_DIR = ROOT / "config"
CAPABILITIES_DIR = Path(os.environ.get("CUA_CAPABILITIES_DIR", ROOT / "capabilities"))
RUNS_DIR = Path(os.environ.get("CUA_RUNS_DIR", ROOT / "runs"))


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_tenant(tenant_id: str) -> TenantConfig:
    return TenantConfig.model_validate(_load_yaml(CONFIG_DIR / "tenants" / f"{tenant_id}.yaml"))


def load_profile(product: str) -> AppProfile:
    return AppProfile.model_validate(_load_yaml(CONFIG_DIR / "apps" / f"{product}.yaml"))


def load_policy(policy_id: str) -> Policy:
    return Policy.model_validate(_load_yaml(CONFIG_DIR / "policy" / f"{policy_id}.yaml"))


class SecretUnavailable(RuntimeError):
    pass


@dataclass
class SecretStore:
    """Resolves ``secret://`` refs for one tenant. Remembers what it handed out
    so the redactor can scrub those exact values from anything persisted."""
    tenant: TenantConfig

    def __post_init__(self) -> None:
        self.issued: list[str] = []

    def get(self, ref: str) -> str:
        env = self.tenant.secrets.get(ref)
        if env is None:
            raise SecretUnavailable(f"tenant {self.tenant.id} has no mapping for secret {ref!r}")
        value = os.environ.get(env)
        if value is None:
            raise SecretUnavailable(f"secret {ref!r}: environment variable {env} is not set")
        self.issued.append(value)
        return value


@dataclass
class Environment:
    """Everything a run needs to know about where it is running."""
    tenant: TenantConfig
    profile: AppProfile
    policy: Policy
    secrets: SecretStore

    @classmethod
    def load(cls, tenant_id: str) -> "Environment":
        tenant = load_tenant(tenant_id)
        return cls(tenant, load_profile(tenant.product), load_policy(tenant.policy), SecretStore(tenant))
