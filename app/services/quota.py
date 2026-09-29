"""Quota arithmetic: UTC calendar-month window, usage aggregation, limit checks.

All datetimes here are naive UTC (see app.models.utcnow). SQLite has no native
timestamp type and drops tzinfo on read, so aware datetimes would silently mix
with naive values and corrupt month-boundary comparisons.

The window is half-open [month_start, month_end) so adjacent months never both
claim an event that lands exactly on midnight.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import UsageEvent

# Sum of the four token buckets. cached_input_tokens counts toward the limit:
# cached tokens are cheaper for the provider but still consume the tenant quota.
TOKEN_COLUMNS = (
    UsageEvent.input_tokens
    + UsageEvent.cached_input_tokens
    + UsageEvent.output_tokens
    + UsageEvent.reasoning_tokens
)


def current_month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return [month_start, month_end) in UTC for the calendar month of `now`.

    month_start is 00:00:00 on the 1st. month_end is 00:00:00 on the 1st of the
    next month, exclusive.
    """
    if now is None:
        now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)
    return month_start, month_end


def monthly_usage(
    db: Session, tenant_id: int, window: tuple[datetime, datetime] | None = None
) -> tuple[int, int]:
    """Return (api_calls, tokens) summed over the tenant's current month."""
    month_start, month_end = window or current_month_window()
    stmt = select(
        func.coalesce(func.sum(UsageEvent.api_calls), 0),
        func.coalesce(func.sum(TOKEN_COLUMNS), 0),
    ).where(
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.created_at >= month_start,
        UsageEvent.created_at < month_end,
    )
    api_calls, tokens = db.execute(stmt).one()
    return int(api_calls), int(tokens)


class QuotaExceeded(Exception):
    """Raised when a request would push the tenant past a plan limit.

    Carries the details the 429 body must report.
    """

    def __init__(self, limit_type: str, used: int, limit: int, requested: int):
        self.limit_type = limit_type
        self.used = used
        self.limit = limit
        self.requested = requested
        super().__init__(f"{limit_type} limit exceeded")

    def to_detail(self) -> dict:
        return {
            "error": "quota_exceeded",
            "message": (
                f"Tenant has exceeded its plan limit for {self.limit_type}. "
                f"Used {self.used} of {self.limit} this month; "
                f"this request needs {self.requested} more."
            ),
            "limit_type": self.limit_type,
            "used": self.used,
            "limit": self.limit,
            "requested": self.requested,
        }


def check_quota(
    api_calls_used: int,
    tokens_used: int,
    api_calls_limit: int,
    tokens_limit: int,
    api_calls_requested: int = 1,
    tokens_requested: int = 0,
) -> None:
    """Raise QuotaExceeded if recording this request would exceed a limit.

    Boundary is exact and inclusive-at-the-limit: a tenant at exactly
    api_calls_limit is allowed, and the next call is rejected. So the check is
    `used + requested > limit`, not `>=`.
    """
    if api_calls_used + api_calls_requested > api_calls_limit:
        raise QuotaExceeded(
            "api_calls", api_calls_used, api_calls_limit, api_calls_requested
        )
    if tokens_used + tokens_requested > tokens_limit:
        raise QuotaExceeded(
            "tokens", tokens_used, tokens_limit, tokens_requested
        )
