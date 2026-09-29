import json

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Tenant
from app.services.usage_service import SubscriptionInactive, meter_request
from app.services.quota import QuotaExceeded

router = APIRouter(tags=["generate"])


class GenerateRequest(BaseModel):
    """Dummy billable action. Token counts are client supplied.

    There is no real model call; this endpoint exists to meter and to exercise
    idempotency and quota enforcement. All four counts must be non-negative
    integers; pydantic rejects negatives and non-integers before we reach the
    database, so a validation failure can never create a usage_events row.
    """

    tenant_id: int = Field(..., gt=0)
    input_tokens: int = Field(0, ge=0)
    cached_input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)
    reasoning_tokens: int = Field(0, ge=0)

    def counts(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@router.post("/generate")
def generate(
    payload: GenerateRequest,
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    db: Session = Depends(get_db),
):
    # The key is client supplied and mandatory. We never synthesise one: a
    # server-generated key cannot make a retry idempotent, which is the entire
    # point of the header.
    if not x_idempotency_key or not x_idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header is required",
        )

    if db.get(Tenant, payload.tenant_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant {payload.tenant_id} does not exist",
        )

    try:
        result = meter_request(
            db=db,
            tenant_id=payload.tenant_id,
            idempotency_key=x_idempotency_key,
            counts=payload.counts(),
        )
    except SubscriptionInactive as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "subscription_inactive",
                "message": (
                    f"Tenant {exc.tenant_id} has no active subscription. "
                    "Billing is required before generating."
                ),
                "subscription_status": exc.status,
            },
        ) from exc
    except QuotaExceeded as exc:
        # No usage_events row was written, so this key stays retryable.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=exc.to_detail()
        ) from exc

    # Serialise here, once, for BOTH the fresh and the replayed path, so the
    # first response and every replay are byte-identical. The stored
    # response_body is written with the same sort_keys=True strategy
    # (app/services/usage_service.py), so what the client first receives is
    # exactly what a retry will receive.
    # A returned tuple would be serialised as a JSON array, so build the
    # Response explicitly rather than returning (body, status).
    payload = json.dumps(result.body, sort_keys=True)
    return Response(
        content=payload,
        status_code=result.status_code,
        media_type="application/json",
    )
