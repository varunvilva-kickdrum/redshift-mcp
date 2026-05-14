"""Environment configuration via pydantic-settings."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_project_root() -> Path:
    """Directory containing pyproject.toml (repo root), for stable .env loading."""
    here = Path(__file__).resolve().parent
    for p in (here, *here.parents):
        if (p / "pyproject.toml").is_file():
            return p
    return Path.cwd()


def _env_file_paths() -> tuple[str, ...]:
    """
    Load .env from project root first so `uv run ...` works from any cwd (e.g. scripts/).

    Optional second file `.env` in cwd allows local overrides when intentionally run elsewhere.
    """
    root = _find_project_root()
    root_env = root / ".env"
    cwd_env = Path.cwd() / ".env"
    paths: list[str] = []
    if root_env.is_file():
        paths.append(str(root_env))
    if cwd_env.is_file() and cwd_env.resolve() != root_env.resolve():
        paths.append(str(cwd_env))
    if not paths:
        paths.append(str(root / ".env"))  # pydantic still ok if missing; env vars only
    return tuple(paths)


def _parse_schema_csv(raw: str | None) -> frozenset[str]:
    if not raw or not raw.strip():
        return frozenset()
    parts = [p.strip().lower() for p in raw.split(",")]
    return frozenset(p for p in parts if p)


class Settings(BaseSettings):
    """Application settings loaded from environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=_env_file_paths(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    redshift_host: str | None = Field(default=None, alias="REDSHIFT_HOST")
    redshift_port: int = Field(default=5439, alias="REDSHIFT_PORT")
    redshift_database: str = Field(alias="REDSHIFT_DATABASE")
    redshift_user: str = Field(alias="REDSHIFT_USER")
    redshift_password: str | None = Field(default=None, alias="REDSHIFT_PASSWORD")

    redshift_iam: bool = Field(default=False, alias="REDSHIFT_IAM")
    redshift_cluster_identifier: str | None = Field(
        default=None,
        alias="REDSHIFT_CLUSTER_IDENTIFIER",
    )
    redshift_aws_region: str | None = Field(default=None, alias="REDSHIFT_AWS_REGION")

    max_rows_returned: int = Field(default=10000, alias="MAX_ROWS_RETURNED")
    query_timeout_seconds: int = Field(default=60, alias="QUERY_TIMEOUT_SECONDS")
    default_schema: str = Field(default="public", alias="DEFAULT_SCHEMA")

    allowed_schemas_raw: str | None = Field(default=None, alias="ALLOWED_SCHEMAS")
    blocked_schemas_raw: str | None = Field(default=None, alias="BLOCKED_SCHEMAS")

    # Parsed schema lists (never loaded directly from env — avoids collisions with ALLOWED_SCHEMAS).
    allowlist_schemas: frozenset[str] = Field(default_factory=frozenset, exclude=True)
    blocklist_schemas: frozenset[str] = Field(default_factory=frozenset, exclude=True)

    # MCP transport / Auth0 (optional; local stdio ignores most of these)
    mcp_transport: str = Field(default="stdio", alias="MCP_TRANSPORT")
    mcp_host: str = Field(default="0.0.0.0", alias="MCP_HOST")
    mcp_port: int = Field(default=8000, alias="MCP_PORT")
    mcp_stateless_http: bool = Field(default=False, alias="MCP_STATELESS_HTTP")
    mcp_json_response: bool = Field(default=True, alias="MCP_JSON_RESPONSE")
    mcp_relax_transport_security: bool = Field(default=False, alias="MCP_RELAX_TRANSPORT_SECURITY")
    mcp_allowed_hosts_csv: str | None = Field(default=None, alias="MCP_ALLOWED_HOSTS")
    mcp_allowed_origins_csv: str | None = Field(default=None, alias="MCP_ALLOWED_ORIGINS")

    auth0_domain: str | None = Field(default=None, alias="AUTH0_DOMAIN")
    auth0_audience: str | None = Field(default=None, alias="AUTH0_AUDIENCE")
    auth0_tier_claim: str = Field(default="https://redshift-mcp/tier", alias="AUTH0_TIER_CLAIM")
    mcp_public_url: AnyHttpUrl | None = Field(default=None, alias="MCP_PUBLIC_URL")

    mcp_local_dev_tier: str = Field(default="analyst", alias="MCP_LOCAL_DEV_TIER")

    rate_limit_dynamodb_table: str | None = Field(default=None, alias="RATE_LIMIT_DYNAMODB_TABLE")
    rate_limit_aws_region: str | None = Field(default=None, alias="RATE_LIMIT_AWS_REGION")
    mcp_cache_max_entries: int = Field(default=512, alias="MCP_CACHE_MAX_ENTRIES")

    @field_validator("allowed_schemas_raw", "blocked_schemas_raw", mode="before")
    @classmethod
    def _strip_optional(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip() or None
        return v

    @field_validator("mcp_transport", mode="before")
    @classmethod
    def _normalize_mcp_transport(cls, v: Any) -> Any:
        if isinstance(v, str):
            vv = v.strip().lower()
            if vv in ("stdio", "streamable-http"):
                return vv
            raise ValueError("MCP_TRANSPORT must be 'stdio' or 'streamable-http'.")
        return v

    @field_validator("mcp_local_dev_tier", mode="before")
    @classmethod
    def _normalize_local_tier(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower() or "analyst"
        return v

    @model_validator(mode="after")
    def _parse_lists_and_auth(self) -> Settings:
        object.__setattr__(self, "allowlist_schemas", _parse_schema_csv(self.allowed_schemas_raw))
        object.__setattr__(self, "blocklist_schemas", _parse_schema_csv(self.blocked_schemas_raw))

        if self.redshift_iam:
            if not self.redshift_cluster_identifier or not self.redshift_cluster_identifier.strip():
                msg = "REDSHIFT_CLUSTER_IDENTIFIER is required when REDSHIFT_IAM=true."
                raise ValueError(msg)
            if not self.redshift_aws_region or not self.redshift_aws_region.strip():
                msg = "REDSHIFT_AWS_REGION is required when REDSHIFT_IAM=true."
                raise ValueError(msg)
        else:
            if not self.redshift_password:
                msg = "REDSHIFT_PASSWORD is required when REDSHIFT_IAM=false."
                raise ValueError(msg)
            if not self.redshift_host or not self.redshift_host.strip():
                msg = "REDSHIFT_HOST is required when REDSHIFT_IAM=false."
                raise ValueError(msg)

        if self.max_rows_returned < 1:
            msg = "MAX_ROWS_RETURNED must be >= 1."
            raise ValueError(msg)
        if self.query_timeout_seconds < 1:
            msg = "QUERY_TIMEOUT_SECONDS must be >= 1."
            raise ValueError(msg)

        if self.mcp_port < 1 or self.mcp_port > 65535:
            msg = "MCP_PORT must be between 1 and 65535."
            raise ValueError(msg)

        if self.auth0_domain and self.auth0_domain.strip():
            if not self.auth0_audience or not str(self.auth0_audience).strip():
                msg = "AUTH0_AUDIENCE is required when AUTH0_DOMAIN is set."
                raise ValueError(msg)
            if self.mcp_public_url is None:
                msg = (
                    "MCP_PUBLIC_URL is required when AUTH0_DOMAIN is set "
                    "(MCP protected resource metadata)."
                )
                raise ValueError(msg)

        if self.mcp_cache_max_entries < 16:
            msg = "MCP_CACHE_MAX_ENTRIES must be >= 16."
            raise ValueError(msg)

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings (singleton per process)."""
    try:
        return Settings()
    except Exception as e:
        cwd = os.getcwd()
        msg = (
            f"Failed to load configuration from environment / `.env` (cwd={cwd}). "
            f"Ensure required variables are set. Original error: {e}"
        )
        raise RuntimeError(msg) from e


def clear_settings_cache() -> None:
    """Clear settings cache (used in tests)."""
    get_settings.cache_clear()
