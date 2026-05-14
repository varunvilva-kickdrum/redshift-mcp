"""Tier mapping rules."""

from __future__ import annotations

import pytest

from redshift_mcp.tiers import tool_allowed


@pytest.mark.parametrize(
    ("tier", "tool", "expected"),
    [
        ("free", "list_schemas", True),
        ("free", "profile_column", False),
        ("free", "run_select_query", False),
        ("premium", "profile_column", True),
        ("premium", "run_select_query", False),
        ("analyst", "run_select_query", True),
    ],
)
def test_tool_allowed(tier: str, tool: str, expected: bool) -> None:
    assert tool_allowed(tier, tool) is expected
