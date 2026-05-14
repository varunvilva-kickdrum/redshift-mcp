"""Per-user hourly rate limits by tier (Free 30, Premium 150, Analyst 500)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redshift_mcp.config import Settings
from redshift_mcp.tiers import TierName

_worker_lock = threading.Lock()
_worker: InMemoryHourlyLimiter | DynamoHourlyLimiter | None = None
_worker_key: tuple[str, str] | None = None


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_sec: int


def _hour_bucket_utc(ts: float | None = None) -> tuple[str, int]:
    """Return (bucket_id, epoch_sec_at_next_hour) for fixed hourly windows."""
    now = datetime.fromtimestamp(ts or time.time(), tz=UTC)
    top_next = now.replace(minute=0, second=0, microsecond=0)
    # next hour boundary
    top_next = top_next + timedelta(hours=1)
    bucket = now.strftime("%Y-%m-%dT%H")
    retry_after = max(1, int(top_next.timestamp() - (ts or time.time())))
    return bucket, retry_after


def tier_hourly_limit(tier: TierName) -> int:
    if tier == "free":
        return 30
    if tier == "premium":
        return 150
    return 500


class InMemoryHourlyLimiter:
    """Thread-safe fixed-window counter per (user_id, UTC hour)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str], int] = {}

    def _gc(self) -> None:
        if len(self._counts) < 10_000:
            return
        # Drop half of keys (best-effort under pathological load).
        keys = list(self._counts.keys())
        for k in keys[: len(keys) // 2]:
            self._counts.pop(k, None)

    def check(self, *, user_id: str, tier: TierName) -> RateLimitResult:
        bucket, retry_after = _hour_bucket_utc()
        limit = tier_hourly_limit(tier)
        key = (user_id, bucket)
        with self._lock:
            self._gc()
            current = self._counts.get(key, 0)
            if current >= limit:
                return RateLimitResult(allowed=False, retry_after_sec=retry_after)
            self._counts[key] = current + 1
        return RateLimitResult(allowed=True, retry_after_sec=0)


class DynamoHourlyLimiter:
    """Distributed hourly counter using DynamoDB (optional for multi-task ECS)."""

    def __init__(self, *, table_name: str, region: str) -> None:
        import boto3  # lazy import

        self._ddb = boto3.client("dynamodb", region_name=region)
        self._table_name = table_name

    def check(self, *, user_id: str, tier: TierName) -> RateLimitResult:
        from botocore.exceptions import ClientError

        bucket, retry_after = _hour_bucket_utc()
        limit = tier_hourly_limit(tier)
        try:
            self._ddb.update_item(
                TableName=self._table_name,
                Key={"pk": {"S": user_id}, "sk": {"S": bucket}},
                UpdateExpression="ADD #c :one",
                ExpressionAttributeNames={"#c": "call_count"},
                ExpressionAttributeValues={":one": {"N": "1"}, ":lim": {"N": str(limit)}},
                ConditionExpression="attribute_not_exists(call_count) OR call_count < :lim",
            )
            return RateLimitResult(allowed=True, retry_after_sec=0)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return RateLimitResult(allowed=False, retry_after_sec=retry_after)
            # Fail-open on DynamoDB errors so the warehouse stays reachable.
            return RateLimitResult(allowed=True, retry_after_sec=0)


def hourly_limiter_for(settings: Settings) -> InMemoryHourlyLimiter | DynamoHourlyLimiter:
    """Return a process-wide limiter (memory by default; DynamoDB when configured)."""
    global _worker, _worker_key
    table = (settings.rate_limit_dynamodb_table or "").strip()
    region = (settings.rate_limit_aws_region or "").strip()
    key = (table, region)
    with _worker_lock:
        if _worker is not None and _worker_key == key:
            return _worker
        if table and region:
            _worker = DynamoHourlyLimiter(table_name=table, region=region)
        else:
            _worker = InMemoryHourlyLimiter()
        _worker_key = key
        return _worker
