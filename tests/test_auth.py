"""Item 4 API-key authentication and tenant-isolation regression tests."""

from unittest.mock import MagicMock, patch

from tests.conftest import key, make_tenant


def test_missing_and_invalid_api_keys_are_rejected(client):
    missing = client.get("/usage/1", headers={"X-API-Key": ""})
    invalid = client.get("/usage/1", headers={"X-API-Key": "not-a-key"})
    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_valid_key_succeeds_and_usage_is_isolated(client):
    tenant_a, key_a = make_tenant(client, email="auth-a@example.com")
    tenant_b, key_b = make_tenant(client, email="auth-b@example.com")

    assert client.get(f"/usage/{tenant_a}", headers={"X-API-Key": key_a}).status_code == 200
    assert client.get(f"/usage/{tenant_b}", headers={"X-API-Key": key_a}).status_code == 403
    assert client.get(f"/usage/{tenant_a}", headers={"X-API-Key": key_b}).status_code == 403


def test_generate_cannot_cross_tenant_boundary(client, session):
    tenant_a, key_a = make_tenant(client, email="generate-a@example.com")
    tenant_b, key_b = make_tenant(client, email="generate-b@example.com")
    body = {"tenant_id": tenant_b, "input_tokens": 1}

    rejected = client.post(
        "/generate",
        json=body,
        headers={"X-API-Key": key_a, "X-Idempotency-Key": key()},
    )
    assert rejected.status_code == 403
    assert client.post(
        "/generate",
        json={"tenant_id": tenant_b, "input_tokens": 1},
        headers={"X-API-Key": key_b, "X-Idempotency-Key": key()},
    ).status_code == 200


def test_checkout_cannot_cross_tenant_boundary(client, stripe_settings):
    tenant_a, key_a = make_tenant(client, email="checkout-a@example.com")
    tenant_b, key_b = make_tenant(client, email="checkout-b@example.com")
    with patch("app.services.stripe_service.stripe") as stripe:
        stripe.Customer.create.return_value = MagicMock(id="cus_auth")
        stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_auth", url="https://checkout.stripe.com/auth"
        )
        assert client.get(
            f"/checkout/{tenant_b}", headers={"X-API-Key": key_a}
        ).status_code == 403
        assert client.get(
            f"/checkout/{tenant_b}", headers={"X-API-Key": key_b}
        ).status_code == 200


def test_webhook_remains_signature_authenticated_only(client, stripe_settings):
    from tests.stripe_helpers import checkout_completed_event, signed_headers

    raw, signature = signed_headers(checkout_completed_event(tenant_id=999999))
    response = client.post(
        "/webhooks/stripe", content=raw, headers={"Stripe-Signature": signature}
    )
    assert response.status_code == 200
