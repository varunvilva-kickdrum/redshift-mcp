"""Tool access tiers (Free / Premium / Analyst) aligned with Auth0 claims."""

from __future__ import annotations

from typing import Literal

TierName = Literal["free", "premium", "analyst"]

TIER_RANK: dict[TierName, int] = {
    "free": 0,
    "premium": 1,
    "analyst": 2,
}

# Minimum tier required to invoke each tool (PDF: premium-only vs analyst raw SQL).
TOOL_MIN_TIER: dict[str, TierName] = {
    "list_schemas": "free",
    "list_tables": "free",
    "describe_table": "free",
    "sample_table": "premium",
    "get_table_size": "premium",
    "profile_column": "premium",
    "run_select_query": "analyst",
}


def normalize_tier(raw: str | None) -> TierName:
    """Map Auth0 / env tier strings to a known tier (default free)."""
    if not raw:
        return "free"
    v = raw.strip().lower()
    if v in TIER_RANK:
        return v  # type: ignore[return-value]
    # Common Auth0 role naming
    if "analyst" in v:
        return "analyst"
    if "premium" in v or "pro" in v:
        return "premium"
    return "free"


def tool_allowed(tier: str, tool_name: str) -> bool:
    t = normalize_tier(tier)
    required = TOOL_MIN_TIER.get(tool_name, "analyst")
    return TIER_RANK[t] >= TIER_RANK[required]
