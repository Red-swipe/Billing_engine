"""Helpers for building real Stripe webhook signatures and payloads.

The signature is a genuine Stripe HMAC-SHA256 over "timestamp.payload", so the
test suite can produce payloads that verify correctly without a Stripe account
or network access. ``stripe.WebhookSignature`` is used directly rather than
reimplementing the algorithm, so the tests exercise the same verification path
production uses.
"""

import json
import time

import stripe

FAKE_WEBHOOK_SECRET = "whsec_FAKE_FOR_UNIT_TESTS"


def signed_headers(payload: dict, secret: str = FAKE_WEBHOOK_SECRET, timestamp=None):
    """Return (raw_body_bytes, stripe_signature_header) for a payload.

    `generate_signature_header` takes the payload as a str and interpolates it
    into "timestamp.payload", so it must be given the decoded text. The returned
    raw bytes are the exact UTF-8 encoding of that same text, which is what
    production re-reads from the request and verifies.
    """
    text = json.dumps(payload)
    raw = text.encode("utf-8")
    ts = timestamp if timestamp is not None else int(time.time())
    header = stripe.WebhookSignature.generate_signature_header(
        payload=text, secret=secret, timestamp=ts
    )
    return raw, header


def checkout_completed_event(
    event_id: str = "evt_test_checkout_1",
    tenant_id: int = 1,
    customer: str = "cus_TESTCUSTOMER",
    subscription: str = "sub_TESTSUB",
) -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_session",
                "object": "checkout.session",
                "customer": customer,
                "subscription": subscription,
                "client_reference_id": str(tenant_id),
                "metadata": {"tenant_id": str(tenant_id), "plan": "Pro"},
            }
        },
    }


def subscription_event(
    event_type: str = "customer.subscription.updated",
    event_id: str = "evt_test_sub_1",
    subscription_id: str = "sub_TESTSUB",
    customer: str = "cus_TESTCUSTOMER",
    status: str = "active",
    tenant_id: int = 1,
) -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "id": subscription_id,
                "object": "subscription",
                "customer": customer,
                "status": status,
                "current_period_start": 1767225600,
                "current_period_end": 1769904000,
                "metadata": {"tenant_id": str(tenant_id)},
            }
        },
    }
