"""Stage 3 tests: Stripe checkout, webhook signature verification, deduplication.

No real Stripe credentials or network access are required. Stripe API calls are
mocked; webhook signatures are genuinely computed with Stripe's own
``WebhookSignature`` helper so the production verification path is exercised.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func, select

from app.models import Plan, StripeEvent, Subscription, Tenant
from tests.conftest import key, make_tenant
from tests.stripe_helpers import (
    FAKE_WEBHOOK_SECRET,
    checkout_completed_event,
    signed_headers,
    subscription_event,
)


def pro_plan(session) -> Plan:
    return session.execute(select(Plan).where(Plan.name == "Pro")).scalar_one()


# --------------------------------------------------------------- Checkout


def test_checkout_creates_session_for_free_tenant(client, session, stripe_settings):
    tenant_id, _ = make_tenant(client)
    fake = MagicMock(id="cs_test_123", url="https://checkout.stripe.com/c/pay/cs_test_123")

    with patch("app.services.stripe_service.stripe") as mock_stripe:
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_test_1")
        mock_stripe.checkout.Session.create.return_value = fake
        resp = client.get(f"/checkout/{tenant_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_123"
    assert body["checkout_session_id"] == "cs_test_123"
    assert body["plan"] == "Pro"
    assert body["price_cents"] == 2000
    # The secret key must never appear in a response.
    assert "sk_test" not in resp.text


def test_checkout_session_created_in_subscription_mode_with_metadata(
    client, session, stripe_settings
):
    tenant_id, _ = make_tenant(client)

    with patch("app.services.stripe_service.stripe") as mock_stripe:
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_test_1")
        mock_stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_1", url="https://checkout.stripe.com/x"
        )
        client.get(f"/checkout/{tenant_id}")

    kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
    assert kwargs["mode"] == "subscription"
    assert kwargs["line_items"][0]["price"] == stripe_settings.STRIPE_PRO_PRICE_ID
    # tenant_id metadata is what makes the webhook able to find the tenant.
    assert kwargs["metadata"]["tenant_id"] == str(tenant_id)
    assert kwargs["client_reference_id"] == str(tenant_id)


def test_customer_created_when_missing_then_reused(client, session, stripe_settings):
    tenant_id, _ = make_tenant(client)
    assert session.get(Tenant, tenant_id).stripe_customer_id is None

    with patch("app.services.stripe_service.stripe") as mock_stripe:
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_created")
        mock_stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_1", url="https://checkout.stripe.com/x"
        )
        client.get(f"/checkout/{tenant_id}")
        assert mock_stripe.Customer.create.call_count == 1
        assert session.get(Tenant, tenant_id).stripe_customer_id == "cus_created"

        # Second call must reuse the persisted customer, not create another.
        client.get(f"/checkout/{tenant_id}")
        assert mock_stripe.Customer.create.call_count == 1, "duplicate customer created"
        assert mock_stripe.checkout.Session.create.call_args.kwargs["customer"] == (
            "cus_created"
        )


def test_existing_customer_id_is_reused_without_api_call(
    client, session, stripe_settings
):
    tenant_id, _ = make_tenant(client)
    tenant = session.get(Tenant, tenant_id)
    tenant.stripe_customer_id = "cus_preexisting"
    session.commit()

    with patch("app.services.stripe_service.stripe") as mock_stripe:
        mock_stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_1", url="https://checkout.stripe.com/x"
        )
        client.get(f"/checkout/{tenant_id}")
        assert mock_stripe.Customer.create.call_count == 0


def test_checkout_unknown_tenant_is_404(client, session, stripe_settings):
    with patch("app.services.stripe_service.stripe"):
        resp = client.get("/checkout/999999")
    assert resp.status_code == 401


def test_checkout_already_pro_returns_409_and_creates_no_session(
    client, session, stripe_settings
):
    tenant_id, _ = make_tenant(client)
    tenant = session.get(Tenant, tenant_id)
    tenant.plan_id = pro_plan(session).id
    sub = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one()
    sub.plan_id = pro_plan(session).id
    sub.status = "active"
    session.commit()

    with patch("app.services.stripe_service.stripe") as mock_stripe:
        resp = client.get(f"/checkout/{tenant_id}")
        assert mock_stripe.checkout.Session.create.call_count == 0

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "already_pro"


def test_checkout_without_stripe_config_is_503(client, session, no_stripe_config):
    tenant_id, _ = make_tenant(client)
    resp = client.get(f"/checkout/{tenant_id}")
    assert resp.status_code == 503


# ----------------------------------------------------- signature security


def test_valid_signature_is_accepted(client, session, stripe_settings):
    tenant_id, _ = make_tenant(client)
    payload = checkout_completed_event(tenant_id=tenant_id)
    raw, header = signed_headers(payload)

    resp = client.post(
        "/webhooks/stripe", content=raw, headers={"Stripe-Signature": header}
    )

    assert resp.status_code == 200
    assert resp.json()["result"] == "processed"


def test_forged_signature_returns_400_and_writes_nothing(
    client, session, stripe_settings
):
    tenant_id, _ = make_tenant(client)
    payload = checkout_completed_event(tenant_id=tenant_id)
    raw, _good = signed_headers(payload)

    before_sub = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one().status
    before_events = session.execute(select(func.count(StripeEvent.id))).scalar_one()
    before_plan = session.get(Tenant, tenant_id).plan_id

    resp = client.post(
        "/webhooks/stripe",
        content=raw,
        headers={"Stripe-Signature": "t=1,v1=deadbeefdeadbeefdeadbeefdeadbeef"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_signature"
    # Nothing at all was written, and nothing changed.
    assert session.execute(select(func.count(StripeEvent.id))).scalar_one() == before_events
    assert session.get(Tenant, tenant_id).plan_id == before_plan
    assert session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one().status == before_sub


def test_missing_signature_header_returns_400(client, session, stripe_settings):
    raw, _ = signed_headers(checkout_completed_event())
    resp = client.post("/webhooks/stripe", content=raw)
    assert resp.status_code == 400
    assert session.execute(select(func.count(StripeEvent.id))).scalar_one() == 0


def test_signature_from_wrong_secret_is_rejected(client, session, stripe_settings):
    raw, header = signed_headers(
        checkout_completed_event(), secret="whsec_ATTACKER_SECRET"
    )
    resp = client.post(
        "/webhooks/stripe", content=raw, headers={"Stripe-Signature": header}
    )
    assert resp.status_code == 400


def test_tampered_body_with_valid_signature_is_rejected(
    client, session, stripe_settings
):
    raw, header = signed_headers(checkout_completed_event(tenant_id=1))
    tampered = raw.replace(b'"tenant_id": "1"', b'"tenant_id": "999"')
    assert tampered != raw
    resp = client.post(
        "/webhooks/stripe", content=tampered, headers={"Stripe-Signature": header}
    )
    assert resp.status_code == 400


# --------------------------------------------------------- deduplication


def test_replay_same_event_twice_processes_once(client, session, stripe_settings):
    tenant_id, _ = make_tenant(client)
    payload = checkout_completed_event(tenant_id=tenant_id)
    raw, header = signed_headers(payload)

    first = client.post(
        "/webhooks/stripe", content=raw, headers={"Stripe-Signature": header}
    )
    second = client.post(
        "/webhooks/stripe", content=raw, headers={"Stripe-Signature": header}
    )

    assert first.status_code == 200 and first.json()["result"] == "processed"
    assert second.status_code == 200 and second.json()["result"] == "duplicate"

    rows = session.execute(select(func.count(StripeEvent.id))).scalar_one()
    assert rows == 1, "duplicate delivery created a second stripe_events row"
    assert (
        session.execute(
            select(func.count(StripeEvent.id)).where(
                StripeEvent.stripe_event_id == "evt_test_checkout_1"
            )
        ).scalar_one()
        == 1
    )


def test_duplicate_delivery_does_not_reapply_business_operation(
    client, session, stripe_settings
):
    tenant_id, _ = make_tenant(client)
    payload = checkout_completed_event(tenant_id=tenant_id)
    raw, header = signed_headers(payload)

    client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": header})
    session.get(Tenant, tenant_id)
    first_sub_id = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one().stripe_subscription_id

    client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": header})
    session.expire_all()
    after_sub_id = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one().stripe_subscription_id

    assert first_sub_id == after_sub_id == "sub_TESTSUB"
    assert session.execute(select(func.count(StripeEvent.id))).scalar_one() == 1


def test_processed_flag_and_timestamp_are_recorded(client, session, stripe_settings):
    tenant_id, _ = make_tenant(client)
    raw, header = signed_headers(checkout_completed_event(tenant_id=tenant_id))
    client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": header})

    row = session.execute(select(StripeEvent)).scalar_one()
    assert row.processed is True
    assert row.processed_at is not None
    assert row.event_type == "checkout.session.completed"
    assert json.loads(row.payload)["id"] == "evt_test_checkout_1"


# ------------------------------------------------------- event handling


def test_checkout_session_completed_upgrades_to_pro(client, session, stripe_settings):
    tenant_id, _ = make_tenant(client)
    raw, header = signed_headers(checkout_completed_event(tenant_id=tenant_id))
    client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": header})

    session.expire_all()
    tenant = session.get(Tenant, tenant_id)
    sub = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one()
    assert tenant.plan.name == "Pro"
    assert tenant.stripe_customer_id == "cus_TESTCUSTOMER"
    assert sub.plan_id == pro_plan(session).id
    assert sub.status == "active"
    assert sub.stripe_subscription_id == "sub_TESTSUB"


def test_upgrade_raises_quota_limit(client, session, stripe_settings):
    """The Pro plan must actually be what /usage and /generate enforce."""
    tenant_id, _ = make_tenant(client)
    raw, header = signed_headers(checkout_completed_event(tenant_id=tenant_id))
    client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": header})

    usage = client.get(f"/usage/{tenant_id}").json()
    assert usage["plan"] == "Pro"
    assert usage["api_calls_limit"] == 50_000
    assert usage["tokens_limit"] == 5_000_000


def test_subscription_updated_syncs_state(client, session, stripe_settings):
    tenant_id, _ = make_tenant(client)
    # Establish the Stripe identity first.
    raw, header = signed_headers(checkout_completed_event(tenant_id=tenant_id))
    client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": header})

    upd = subscription_event(
        event_type="customer.subscription.updated",
        event_id="evt_test_sub_upd",
        status="active",
    )
    raw2, header2 = signed_headers(upd)
    resp = client.post(
        "/webhooks/stripe", content=raw2, headers={"Stripe-Signature": header2}
    )
    assert resp.status_code == 200 and resp.json()["result"] == "processed"

    session.expire_all()
    sub = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one()
    assert sub.status == "active"
    assert sub.current_period_end is not None


def test_subscription_deleted_marks_inactive_and_blocks_generate(
    client, session, stripe_settings
):
    tenant_id, _ = make_tenant(client)
    raw, header = signed_headers(checkout_completed_event(tenant_id=tenant_id))
    client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": header})

    dele = subscription_event(
        event_type="customer.subscription.deleted",
        event_id="evt_test_sub_del",
        status="canceled",
    )
    raw2, header2 = signed_headers(dele)
    resp = client.post(
        "/webhooks/stripe", content=raw2, headers={"Stripe-Signature": header2}
    )
    assert resp.status_code == 200 and resp.json()["result"] == "processed"

    session.expire_all()
    sub = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one()
    assert sub.status == "canceled"
    # The row is kept, not deleted.
    assert session.execute(select(func.count(Subscription.id))).scalar_one() == 1

    # A canceled tenant can no longer generate: Stage 2's 402 still applies.
    gen = client.post(
        "/generate",
        json={"tenant_id": tenant_id, "input_tokens": 1},
        headers={"X-Idempotency-Key": key()},
    )
    assert gen.status_code == 402


def test_event_for_unknown_tenant_is_ignored_not_applied(
    client, session, stripe_settings
):
    tenant_id, _ = make_tenant(client)
    before = session.get(Tenant, tenant_id).plan_id

    payload = checkout_completed_event(event_id="evt_orphan", tenant_id=424242)
    raw, header = signed_headers(payload)
    resp = client.post(
        "/webhooks/stripe", content=raw, headers={"Stripe-Signature": header}
    )

    assert resp.status_code == 200
    assert resp.json()["result"] == "ignored"
    session.expire_all()
    assert session.get(Tenant, tenant_id).plan_id == before

    # Still recorded, but explicitly not applied.
    row = session.execute(select(StripeEvent)).scalar_one()
    assert row.processed is False and row.processed_at is None


def test_webhook_cannot_upgrade_a_different_tenant(
    client, session, stripe_settings
):
    """Data-integrity rule: an event must only ever touch its own tenant."""
    target, _ = make_tenant(client, email="target@example.com")
    other, _ = make_tenant(client, email="other@example.com")
    other_before = session.get(Tenant, other).plan_id

    raw, header = signed_headers(checkout_completed_event(tenant_id=target))
    client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": header})

    session.expire_all()
    assert session.get(Tenant, target).plan.name == "Pro"
    assert session.get(Tenant, other).plan_id == other_before


def test_end_to_end_free_to_pro_then_generate(client, session, stripe_settings):
    """Free -> checkout -> completed -> Pro quota enforced by /generate."""
    tenant_id, _ = make_tenant(client)
    assert client.get(f"/usage/{tenant_id}").json()["api_calls_limit"] == 1_000

    with patch("app.services.stripe_service.stripe") as mock_stripe:
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_e2e")
        mock_stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_e2e", url="https://checkout.stripe.com/c/e2e"
        )
        checkout = client.get(f"/checkout/{tenant_id}")
    assert checkout.status_code == 200

    raw, header = signed_headers(
        checkout_completed_event(
            event_id="evt_e2e", tenant_id=tenant_id,
            customer="cus_e2e", subscription="sub_e2e",
        )
    )
    client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": header})

    usage = client.get(f"/usage/{tenant_id}").json()
    assert usage["plan"] == "Pro"
    assert usage["api_calls_limit"] == 50_000

    gen = client.post(
        "/generate",
        json={"tenant_id": tenant_id, "input_tokens": 100},
        headers={"X-Idempotency-Key": key()},
    )
    assert gen.status_code == 200


# ---------------------------------------------------- Stage 2 regression


def test_stage2_generate_idempotency_still_holds(client, session, stripe_settings):
    tenant_id, _ = make_tenant(client)
    k = key()
    body = {"tenant_id": tenant_id, "input_tokens": 10, "output_tokens": 5}
    a = client.post("/generate", json=body, headers={"X-Idempotency-Key": k})
    b = client.post("/generate", json=body, headers={"X-Idempotency-Key": k})
    assert a.status_code == 200 and b.status_code == 200
    assert a.content == b.content
    assert session.execute(
        select(func.count()).select_from(
            select(StripeEvent).subquery()
        )
    ).scalar_one() == 0
