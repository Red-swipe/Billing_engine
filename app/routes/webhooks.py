"""POST /webhooks/stripe — raw-body, signature-verified event receiver.

The raw bytes are read directly from the request because Stripe's signature is
computed over the exact payload sent. Letting FastAPI parse and re-serialise the
JSON first would change the bytes and break verification.
"""

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.stripe_service import (
    WebhookSignatureError,
    construct_event,
    process_event,
)

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    signature = request.headers.get("Stripe-Signature", "")

    # Verify before parsing, before touching the database. A forged or tampered
    # request writes nothing at all.
    try:
        event = construct_event(raw_body, signature)
    except WebhookSignatureError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_signature", "message": str(exc)},
        )

    result = process_event(db, event, raw_body)

    # Always 200 for a verified event, including duplicates: Stripe retries
    # anything that is not 2xx, and a duplicate is a success, not a failure.
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "received": True,
            "stripe_event_id": result["stripe_event_id"],
            "event_type": result["event_type"],
            "result": result["status"],
        },
    )
