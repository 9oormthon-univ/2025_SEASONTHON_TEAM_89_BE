"""Offline billing regressions: SQLite, synthetic users, and recording Google doubles.

The production database is replaced before any app import. Socket connections
and real HTTP transports are forbidden, including during module initialization.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import socket
import sys
import types
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool


UTC = timezone.utc
NOW = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)
USER_A = "00000000-0000-4000-8000-000000000001"
USER_B = "00000000-0000-4000-8000-000000000002"
MONTHLY = "ad_free_monthly"
YEARLY = "ad_free_yearly"
LIFETIME = "ad_free_lifetime"
TOKEN = "synthetic-receipt-token-for-offline-billing"
PUSH_BEARER = "Bearer synthetic-google-push-id-token"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def subscription(user=USER_A, *, product=MONTHLY, state="ACTIVE", expiry=None,
                 acknowledged=False, linked=None):
    payload = {
        "subscriptionState": "SUBSCRIPTION_STATE_" + state,
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED" if acknowledged else "ACKNOWLEDGEMENT_STATE_PENDING",
        "externalAccountIdentifiers": {"obfuscatedExternalAccountId": digest(user)},
        "lineItems": [{"productId": product, "expiryTime": stamp(expiry or NOW + timedelta(days=30)),
                       "latestSuccessfulOrderId": "GPA.synthetic-fixture.0",
                       "autoRenewingPlan": {"autoRenewEnabled": state != "CANCELED"},
                       "offerDetails": {"basePlanId": "yearly" if product == YEARLY else "monthly"}}],
    }
    if linked:
        payload["linkedPurchaseToken"] = linked
    return payload


def lifetime(user=USER_A, *, state="PURCHASED", acknowledged=False):
    payload = {
        "purchaseStateContext": {"purchaseState": state},
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED" if acknowledged else "ACKNOWLEDGEMENT_STATE_PENDING",
        "obfuscatedExternalAccountId": digest(user),
        "orderId": "GPA.synthetic-lifetime-fixture",
        "productLineItem": [{"productId": LIFETIME, "productOfferDetails": {
            "purchaseOptionId": "buy", "quantity": 1, "refundableQuantity": 1,
            "consumptionState": "CONSUMPTION_STATE_YET_TO_BE_CONSUMED"}}],
    }
    if state == "PURCHASED":
        payload["purchaseCompletionTime"] = stamp(NOW - timedelta(minutes=1))
    return payload


class RecordingPlay:
    def __init__(self, error_type):
        self.error_type = error_type
        self.receipts, self.calls, self.ack_calls = {}, [], []
        self.ack_error = False
        self.ready = True
        self.pages = {None: {"voidedPurchases": []}}

    def _get(self, token, kind):
        self.calls.append((kind, token))
        result = self.receipts[token]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def get_subscription(self, token):
        return self._get(token, "subs")

    def get_product(self, token):
        return self._get(token, "inapp")

    def acknowledge(self, product_id, product_type, token):
        self.ack_calls.append((product_id, product_type, token))
        if self.ack_error:
            raise self.error_type("unavailable")
        self.receipts[token]["acknowledgementState"] = "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"

    def catalog_ready(self):
        return self.ready

    def list_voided(self, start_ms, end_ms, page_token=None):
        self.calls.append(("voided", start_ms, end_ms, page_token))
        result = self.pages[page_token]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


@pytest.fixture(scope="module")
def isolated_modules():
    patch = pytest.MonkeyPatch()
    old_modules = {name: module for name, module in sys.modules.items()
                   if name == "app" or name.startswith("app.")}
    for name in old_modules:
        sys.modules.pop(name)
    patch.setenv("ENVIRONMENT", "test")
    patch.setenv("JWT_SECRET_KEY", "public-offline-billing-fixture-secret-never-used-in-production")
    patch.setenv("AUTH_KEY_PATH", "")
    patch.setenv("FIREBASE_CREDENTIALS_PATH", "")
    patch.setenv("KAKAO_ADMIN_KEY", "")
    attempted_network = []

    def deny_network(*args, **kwargs):
        attempted_network.append("unexpected external transport")
        raise AssertionError("External network is forbidden in offline billing tests")

    patch.setattr(socket.socket, "connect", deny_network)
    patch.setattr(socket.socket, "connect_ex", deny_network)
    patch.setattr(socket, "create_connection", deny_network)
    patch.setattr(httpx.HTTPTransport, "handle_request", deny_network)
    base, runtime = declarative_base(), SimpleNamespace(factory=None)

    def get_db():
        assert runtime.factory is not None
        with runtime.factory() as db:
            yield db

    database = types.ModuleType("app.core.database")
    database.Base, database.get_db = base, get_db
    database.SessionLocal = lambda: runtime.factory()
    database.engine = None
    patch.setitem(sys.modules, "app.core.database", database)
    try:
        importlib.import_module("app")
        modules = SimpleNamespace(
            adapter=importlib.import_module("app.services.play_billing_client"),
            service=importlib.import_module("app.services.play_billing_service"),
            models=importlib.import_module("app.models.play_billing"),
            routes=importlib.import_module("app.api.endpoints.billing"),
            jwt=importlib.import_module("app.services.jwt_service").jwt_service,
            User=importlib.import_module("app.models.user").User,
            base=base, runtime=runtime, get_db=get_db,
        )
        yield modules
    finally:
        runtime.factory = None
        patch.undo()
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name)
        sys.modules.update(old_modules)
        assert attempted_network == [], "A billing test attempted an external connection"


@pytest.fixture
def api(isolated_modules, monkeypatch, tmp_path):
    m = isolated_modules
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    m.runtime.factory = factory
    with factory() as db:
        db.add_all([m.User(user_id=USER_A, kakao_id="synthetic-billing-a", nickname="Billing fixture A", is_active=True),
                    m.User(user_id=USER_B, kakao_id="synthetic-billing-b", nickname="Billing fixture B", is_active=True)])
        db.commit()
    clock = SimpleNamespace(now=NOW)
    play = RecordingPlay(m.adapter.PlayError)
    codec = m.service.TokenCipher(Fernet.generate_key())
    credentials_file, key_file = tmp_path / "unused-google-fixture.json", tmp_path / "fixture-fernet-key"
    credentials_file.write_text("{}", encoding="utf-8")
    key_file.write_bytes(Fernet.generate_key())
    settings = m.adapter.BillingSettings(
        enabled=True, credentials_file=str(credentials_file), token_key_file=str(key_file),
        rtdn_audience="https://fixture.invalid/api/billing/google-play/rtdn",
        rtdn_email="push-fixture@offline-fixture.iam.gserviceaccount.com",
        rtdn_subscription="projects/offline-fixture/subscriptions/billing-fixture",
    )
    state = SimpleNamespace(m=m, engine=engine, factory=factory, clock=clock,
                            play=play, codec=codec, settings=settings)
    monkeypatch.setattr(m.routes, "runtime", lambda settings: (play, codec))
    monkeypatch.setattr(m.routes, "_catalog_cache", None)
    original_service_for = m.routes.service_for

    def service_for(db, settings, **kwargs):
        service = original_service_for(db, settings, **kwargs)
        service.now = lambda: clock.now
        return service

    monkeypatch.setattr(m.routes, "service_for", service_for)
    test_app = FastAPI()
    test_app.include_router(m.routes.router, prefix="/api/billing")
    test_app.dependency_overrides[m.routes.get_settings] = lambda: state.settings
    state.headers = {
        user: {"Authorization": "Bearer " + m.jwt.create_access_token({"user_id": user})}
        for user in (USER_A, USER_B)
    }
    with TestClient(test_app, raise_server_exceptions=False) as client:
        state.client = client
        yield state
    m.runtime.factory = None
    engine.dispose()


def service(api, db):
    return api.m.service.BillingService(db, api.play, api.codec, now=lambda: api.clock.now)


def verify_body(*, token=TOKEN, product=MONTHLY, kind="subs", **extra):
    return {"package_name": "com.weheome.app", "product_id": product,
            "product_type": kind, "purchase_token": token, **extra}


def verify_http(api, *, user=USER_A, **kwargs):
    return api.client.post("/api/billing/google-play/verify", json=verify_body(**kwargs),
                           headers=api.headers[user])


@pytest.mark.parametrize("state,active,completed", [
    ("ACTIVE", True, True), ("IN_GRACE_PERIOD", True, True), ("CANCELED", True, True),
    ("PAUSED", False, True), ("ON_HOLD", False, True), ("EXPIRED", False, True),
    ("PENDING", False, False), ("PENDING_PURCHASE_CANCELED", False, False),
])
def test_subscription_states_preserve_only_paid_access(isolated_modules, state, active, completed):
    result = isolated_modules.adapter.snapshot(subscription(state=state, linked="old-receipt-token"), MONTHLY, "subs", NOW)
    assert (result.active, result.completed, result.acknowledged) == (active, completed, False)
    assert result.expires_at.tzinfo == UTC
    if not completed:
        assert result.linked_token is None
    assert result.subscription_state == state
    assert result.auto_renewing is (state != "CANCELED")
    assert result.next_billing_at == (result.expires_at if state == "ACTIVE" else None)


@pytest.mark.parametrize("value,expected", [(True, True), (False, False),
                                           (None, None), (1, None), ("true", None)])
def test_renewal_display_requires_an_explicit_google_boolean(isolated_modules, value, expected):
    payload = subscription(acknowledged=True)
    payload["lineItems"][0]["autoRenewingPlan"]["autoRenewEnabled"] = value
    result = isolated_modules.adapter.snapshot(payload, MONTHLY, "subs", NOW)
    assert result.active  # Optional display data never creates the paid entitlement.
    assert result.auto_renewing is expected
    assert result.next_billing_at == (result.expires_at if expected is True else None)


def test_missing_renewal_flag_never_invents_a_charge_date(isolated_modules):
    payload = subscription(acknowledged=True)
    payload["lineItems"][0]["autoRenewingPlan"].pop("autoRenewEnabled")
    result = isolated_modules.adapter.snapshot(payload, MONTHLY, "subs", NOW)
    assert result.auto_renewing is None and result.next_billing_at is None


@pytest.mark.parametrize("offset", [0, -1])
def test_subscription_expiry_is_exclusive(isolated_modules, offset):
    result = isolated_modules.adapter.snapshot(subscription(expiry=NOW + timedelta(seconds=offset)), MONTHLY, "subs", NOW)
    assert result.completed and not result.active


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(subscriptionState="UNKNOWN"),
    lambda p: p.update(subscriptionState={}),
    lambda p: p.update(acknowledgementState={}),
    lambda p: p.update(externalAccountIdentifiers={}),
    lambda p: p["externalAccountIdentifiers"].update(obfuscatedExternalAccountId="A" * 64),
    lambda p: p["lineItems"][0].update(productId=YEARLY),
    lambda p: p["lineItems"][0]["offerDetails"].update(basePlanId="unapproved"),
    lambda p: p["lineItems"][0]["offerDetails"].update(offerId="free-trial"),
    lambda p: p["lineItems"][0].update(prepaidPlan={}),
    lambda p: p["lineItems"][0]["autoRenewingPlan"].update(installmentDetails={}),
    lambda p: p["lineItems"][0].update(expiryTime="2026-11-13T00:00:00"),
    lambda p: p["lineItems"].append(deepcopy(p["lineItems"][0])),
])
def test_subscription_receipt_mismatches_fail_closed(isolated_modules, mutation):
    payload = subscription()
    mutation(payload)
    with pytest.raises(isolated_modules.adapter.PlayError, match="^invalid_purchase$"):
        isolated_modules.adapter.snapshot(payload, MONTHLY, "subs", NOW)


@pytest.mark.parametrize("state,active", [("PURCHASED", True), ("PENDING", False), ("CANCELLED", False)])
def test_lifetime_states_never_grant_pending_purchase(isolated_modules, state, active):
    result = isolated_modules.adapter.snapshot(lifetime(state=state), LIFETIME, "inapp", NOW)
    assert result.active is active and result.completed is (state != "PENDING")
    assert result.revoked is (state == "CANCELLED")
    assert result.expires_at is None and result.linked_token is None


@pytest.mark.parametrize("key,value", [
    ("quantity", 2), ("quantity", True), ("quantity", "1"), ("refundableQuantity", 2),
    ("consumptionState", "CONSUMPTION_STATE_CONSUMED"), ("purchaseOptionId", "rent"),
    ("offerId", "discount"), ("rentOfferDetails", {}), ("preorderOfferDetails", {}),
])
def test_lifetime_rejects_consumed_refunded_or_other_options(isolated_modules, key, value):
    payload = lifetime()
    payload["productLineItem"][0]["productOfferDetails"][key] = value
    with pytest.raises(isolated_modules.adapter.PlayError, match="^invalid_purchase$"):
        isolated_modules.adapter.snapshot(payload, LIFETIME, "inapp", NOW)


def test_pending_receipt_cannot_be_acknowledged(isolated_modules):
    for payload, product, kind in [(subscription(state="PENDING", acknowledged=True), MONTHLY, "subs"),
                                   (lifetime(state="PENDING", acknowledged=True), LIFETIME, "inapp")]:
        with pytest.raises(isolated_modules.adapter.PlayError):
            isolated_modules.adapter.snapshot(payload, product, kind, NOW)


def test_full_refund_is_a_trusted_terminal_snapshot(isolated_modules):
    payload = lifetime(acknowledged=True)
    payload["productLineItem"][0]["productOfferDetails"]["refundableQuantity"] = 0
    result = isolated_modules.adapter.snapshot(payload, LIFETIME, "inapp", NOW)
    assert result.completed and result.revoked and not result.active


class OfflineCredentials:
    def before_request(self, auth_request, method, url, headers):
        headers["Authorization"] = "Bearer synthetic-access-token"


def test_google_transport_quotes_receipt_uses_deadline_and_never_logs(isolated_modules, caplog):
    m, requests = isolated_modules.adapter, []

    def record(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 8.0
        if request.method == "POST":
            assert request.content == b"{}"
            return httpx.Response(204)
        return httpx.Response(200, json={})

    caplog.set_level("DEBUG", logger="httpx")
    with httpx.Client(transport=httpx.MockTransport(record)) as http:
        play = m.PlayClient(m.BillingSettings(), http_client=http, credentials=OfflineCredentials())
        play.get_product(TOKEN + "/?private=#value")
        play.get_subscription(TOKEN)
        play.acknowledge(LIFETIME, "inapp", TOKEN)
        play.acknowledge(MONTHLY, "subs", TOKEN)
        play.list_voided(10, 20, "synthetic-page/token")
    assert b"%2F%3Fprivate%3D%23value" in requests[0].url.raw_path
    assert requests[0].url.path.startswith("/androidpublisher/v3/applications/com.weheome.app/purchases/productsv2/tokens/")
    assert requests[1].url.path.endswith("/purchases/subscriptionsv2/tokens/" + TOKEN)
    assert requests[-1].url.params["type"] == "1"
    assert requests[-1].url.params["token"] == "synthetic-page/token"
    assert TOKEN not in caplog.text and "synthetic-access-token" not in caplog.text


@pytest.mark.parametrize("status,code", [(301, "unavailable"), (400, "invalid_purchase"),
                                       (404, "not_found"), (410, "invalid_purchase"),
                                       (403, "unavailable"), (503, "unavailable")])
def test_google_http_errors_are_safe_and_redirects_not_followed(isolated_modules, status, code):
    m, requests = isolated_modules.adapter, []

    def response(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.invalid/"}, json={"secret": TOKEN})

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        play = m.PlayClient(m.BillingSettings(), http_client=http, credentials=OfflineCredentials())
        with pytest.raises(m.PlayError) as error:
            play.get_product(TOKEN)
    assert error.value.code == code and str(error.value) == code
    assert len(requests) == 1


def test_explicit_service_account_oauth_refresh_uses_mock_transport(api, tmp_path, caplog):
    """Exercise real google-auth signing with a newly generated test-only RSA key."""
    m = api.m.adapter
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()).decode("ascii")
    key_path = tmp_path / "synthetic-service-account.json"
    key_path.write_text(json.dumps({
        "type": "service_account", "project_id": "offline-fixture", "private_key_id": "synthetic-key-id",
        "private_key": pem, "client_email": "play-fixture@offline-fixture.iam.gserviceaccount.com",
        "client_id": "123456789", "token_uri": "https://oauth2.googleapis.com/token",
    }), encoding="utf-8")
    seen = []

    def google(request):
        seen.append(request)
        assert request.extensions["timeout"]["read"] == 8.0
        if request.url.host == "oauth2.googleapis.com":
            assert request.method == "POST" and request.url.path == "/token"
            assertion = parse_qs(request.content.decode())["assertion"][0]
            google_jwt = importlib.import_module("google.auth.jwt")
            claims = google_jwt.decode(assertion, verify=False)
            assert claims["scope"] == m.ANDROIDPUBLISHER_SCOPE
            assert claims["iss"] == "play-fixture@offline-fixture.iam.gserviceaccount.com"
            return httpx.Response(200, json={"access_token": "synthetic-refreshed-token", "token_type": "Bearer", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer synthetic-refreshed-token"
        return httpx.Response(200, json={})

    caplog.set_level("DEBUG", logger="httpx")
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        play = m.PlayClient(replace(api.settings, credentials_file=str(key_path)), http_client=http)
        assert play._credentials is None and seen == []
        play.get_subscription(TOKEN)
        play.get_product(TOKEN)
        assert play._credentials.scopes == [m.ANDROIDPUBLISHER_SCOPE]
    assert [request.url.host for request in seen] == ["oauth2.googleapis.com", "androidpublisher.googleapis.com", "androidpublisher.googleapis.com"]
    assert TOKEN not in caplog.text and "synthetic-refreshed-token" not in caplog.text and pem not in caplog.text


def catalog_payload(m, product):
    spec = m.PRODUCTS[product]
    data = {"packageName": m.PACKAGE_NAME, "productId": product}
    price = {"currencyCode": "KRW", "units": str(spec.krw_price), "nanos": 0}
    if spec.product_type == "subs":
        data["basePlans"] = [{"basePlanId": spec.base_plan_id, "state": "ACTIVE",
                              "autoRenewingBasePlanType": {"billingPeriodDuration": spec.billing_period},
                              "regionalConfigs": [{"regionCode": "KR", "newSubscriberAvailability": True, "price": price}]}]
    else:
        data["purchaseOptions"] = [{"purchaseOptionId": "buy", "state": "ACTIVE",
                                   "buyOption": {"multiQuantityEnabled": False},
                                   "regionalPricingAndAvailabilityConfigs": [{"regionCode": "KR", "availability": "AVAILABLE", "price": price}]}]
    return data


@pytest.mark.parametrize("damage", [None, "price", "period", "inactive", "quantity", "rent", "option"])
def test_catalog_requires_exact_active_catalog(api, damage):
    m = api.m.adapter
    catalog = {product: catalog_payload(m, product) for product in m.PRODUCTS}
    if damage == "price":
        catalog[MONTHLY]["basePlans"][0]["regionalConfigs"][0]["price"]["units"] = "991"
    elif damage == "period":
        catalog[YEARLY]["basePlans"][0]["autoRenewingBasePlanType"]["billingPeriodDuration"] = "P1M"
    elif damage == "inactive":
        catalog[MONTHLY]["basePlans"][0]["state"] = "INACTIVE"
    elif damage == "quantity":
        catalog[LIFETIME]["purchaseOptions"][0]["buyOption"]["multiQuantityEnabled"] = True
    elif damage == "rent":
        catalog[LIFETIME]["purchaseOptions"][0]["rentOption"] = {}
    elif damage == "option":
        catalog[LIFETIME]["purchaseOptions"][0]["purchaseOptionId"] = "other-buy"
    requested = []

    def metadata(request):
        requested.append(request.url.path)
        return httpx.Response(200, json=catalog[request.url.path.rsplit("/", 1)[-1]])

    with httpx.Client(transport=httpx.MockTransport(metadata)) as http:
        play = m.PlayClient(api.settings, http_client=http, credentials=OfflineCredentials())
        assert play.catalog_ready() is (damage is None)
    if damage is None:
        assert len(requested) == 3 and "/oneTimeProducts/" in requested[-1]


def test_readonly_catalog_inspection_does_not_require_checkout_enabled(api):
    m, requested = api.m.adapter, []

    def metadata(request):
        requested.append(request.url.path)
        return httpx.Response(200, json=catalog_payload(m, request.url.path.rsplit("/", 1)[-1]))

    with httpx.Client(transport=httpx.MockTransport(metadata)) as http:
        play = m.PlayClient(replace(api.settings, enabled=False), http_client=http, credentials=OfflineCredentials())
        assert play.catalog_ready()
    assert len(requested) == 3


def test_receipt_is_encrypted_idempotent_and_bound_to_one_account(api):
    api.play.receipts[TOKEN] = subscription()
    first, second = verify_http(api), verify_http(api)
    assert first.status_code == second.status_code == 200
    assert first.json()["acknowledged"] and first.json()["entitlement"]["ad_free"]
    assert first.json()["purchase_token_sha256"] == digest(TOKEN)
    assert len(api.play.ack_calls) == 1
    with api.factory() as db:
        record = db.get(api.m.models.BillingPurchase, digest(TOKEN))
        assert TOKEN not in record.token_encrypted
        assert api.codec.decrypt(record.token_encrypted) == TOKEN
        assert db.query(api.m.models.BillingPurchase).count() == 1
    calls = len(api.play.calls)
    api.play.receipts[TOKEN] = subscription(USER_B)
    response = verify_http(api, user=USER_B)
    assert response.status_code == 409 and response.json()["detail"] == "purchase_account_mismatch"
    assert len(api.play.calls) == calls and TOKEN not in response.text


def test_google_account_mapping_must_match_jwt_actor(api):
    api.play.receipts[TOKEN] = subscription(USER_B)
    response = verify_http(api)
    assert response.status_code == 403
    assert not api.play.ack_calls
    with api.factory() as db:
        assert db.query(api.m.models.BillingPurchase).count() == 0


@pytest.mark.parametrize("state,expected", [("ACTIVE", True), ("IN_GRACE_PERIOD", True),
                                          ("CANCELED", True), ("PAUSED", False),
                                          ("ON_HOLD", False), ("EXPIRED", False)])
def test_service_applies_google_subscription_state(api, state, expected):
    api.play.receipts[TOKEN] = subscription(state=state)
    response = verify_http(api)
    assert response.status_code == 200, response.text
    assert response.json()["entitlement"]["ad_free"] is expected
    assert bool(api.play.ack_calls) is expected


@pytest.mark.parametrize("state", ["PENDING", "PENDING_PURCHASE_CANCELED"])
def test_pending_replacement_neither_grants_nor_supersedes(api, state):
    old = TOKEN + "-old"
    api.play.receipts[old] = subscription(acknowledged=True)
    assert verify_http(api, token=old).status_code == 200
    api.play.receipts[TOKEN] = subscription(state=state, linked=old)
    response = verify_http(api)
    assert response.status_code == 409 and response.json()["detail"] == "purchase_pending"
    assert not api.play.ack_calls
    with api.factory() as db:
        record = db.get(api.m.models.BillingPurchase, digest(old))
        assert record.active and record.superseded_by_hash is None
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)) is None


def test_unseen_linked_predecessor_is_reserved_against_replay(api):
    old = TOKEN + "-unseen-old"
    api.play.receipts[TOKEN] = subscription(linked=old)
    assert verify_http(api).status_code == 200
    with api.factory() as db:
        tombstone = db.get(api.m.models.BillingPurchase, digest(old))
        assert tombstone.user_id == USER_A and not tombstone.active
        assert tombstone.superseded_by_hash == digest(TOKEN)
    api.play.receipts[old] = subscription(USER_B)
    assert verify_http(api, user=USER_B, token=old).status_code == 409
    api.play.receipts[old] = subscription(acknowledged=True)
    response = verify_http(api, token=old)
    assert response.status_code == 200
    with api.factory() as db:
        assert not db.get(api.m.models.BillingPurchase, digest(old)).active


def test_replacement_cannot_take_another_accounts_predecessor(api):
    old = TOKEN + "-owned-by-b"
    api.play.receipts[old] = subscription(USER_B, acknowledged=True)
    assert verify_http(api, user=USER_B, token=old).status_code == 200
    api.play.receipts[TOKEN] = subscription(linked=old)
    response = verify_http(api)
    assert response.status_code == 409
    with api.factory() as db:
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)) is None
        assert db.get(api.m.models.BillingPurchase, digest(old)).active
    assert not api.play.ack_calls


def test_acknowledgement_failure_has_no_paid_lease_and_retry_recovers(api):
    api.play.receipts[TOKEN] = subscription()
    api.play.ack_error = True
    response = verify_http(api)
    assert response.status_code == 503
    assert response.json()["detail"] == "purchase_acknowledgement_pending"
    with api.factory() as db:
        record = db.get(api.m.models.BillingPurchase, digest(TOKEN))
        assert not record.active and not record.acknowledged and record.checked_at is None
    assert api.client.get("/api/billing/entitlement", headers=api.headers[USER_A]).status_code == 503
    api.play.ack_error = False
    response = verify_http(api)
    assert response.status_code == 200 and response.json()["entitlement"]["ad_free"]


def test_lease_rechecks_at_five_minutes_and_does_not_extend_on_outage(api):
    api.play.receipts[TOKEN] = subscription(acknowledged=True)
    initial = verify_http(api).json()["entitlement"]
    assert initial["cache_until"] == stamp(NOW + timedelta(minutes=5))
    api.play.receipts[TOKEN] = api.m.adapter.PlayError("unavailable")
    api.clock.now = NOW + timedelta(minutes=4, seconds=59)
    fresh = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A])
    assert fresh.status_code == 200 and fresh.json()["cache_until"] == initial["cache_until"]
    assert fresh.json()["ad_free"]
    assert fresh.json()["next_billing_at"] is None
    assert fresh.json()["auto_renewing"] is None
    assert fresh.json()["subscription_state"] is None
    assert len(api.play.calls) == 2  # Optional plan display re-read failed; original lease still bounds access.
    api.clock.now = NOW + timedelta(minutes=5)
    assert api.client.get("/api/billing/entitlement", headers=api.headers[USER_A]).status_code == 503
    with api.factory() as db:
        checked = api.m.service.utc(db.get(api.m.models.BillingPurchase, digest(TOKEN)).checked_at)
        assert checked == NOW


def test_subscription_lease_is_clipped_to_expiry_and_renewal_uses_google_time(api):
    expiry = NOW + timedelta(seconds=30)
    api.play.receipts[TOKEN] = subscription(acknowledged=True, expiry=expiry)
    initial = verify_http(api).json()["entitlement"]
    assert initial["expires_at"] == initial["cache_until"] == stamp(expiry)
    next_expiry = NOW + timedelta(days=31)
    api.play.receipts[TOKEN] = subscription(acknowledged=True, expiry=next_expiry)
    # Renewals at the old expiry must be rechecked before the five-minute cache elapses.
    api.clock.now = expiry
    renewed = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A])
    assert renewed.status_code == 200 and renewed.json()["ad_free"]
    assert renewed.json()["expires_at"] == stamp(next_expiry)
    assert len(api.play.calls) == 2


def test_verified_subscription_exposes_google_renewal_without_a_second_fetch(api):
    expiry = NOW + timedelta(days=30)
    api.play.receipts[TOKEN] = subscription(acknowledged=True, expiry=expiry)
    response = verify_http(api)
    assert response.status_code == 200, response.text
    plan = response.json()["entitlement"]
    assert plan["ad_free"] and plan["subscription_state"] == "ACTIVE"
    assert plan["auto_renewing"] is True and plan["next_billing_at"] == stamp(expiry)
    assert len(api.play.calls) == 1


def test_acknowledgement_wait_cannot_attach_old_active_metadata_to_a_cancel_rtdn(api, monkeypatch):
    expiry = NOW + timedelta(days=30)
    api.play.receipts[TOKEN] = subscription(expiry=expiry)
    original = api.play.acknowledge

    def acknowledge(*args):
        # Exact same clock/expiry models MySQL DATETIME's one-second precision.
        # This different request stores a newer CANCELED state during ACK I/O.
        api.play.receipts[TOKEN] = subscription(state="CANCELED", acknowledged=True, expiry=expiry)
        with api.factory() as db:
            service(api, db).reconcile_notification(TOKEN, "subs")
            canceled = db.get(api.m.models.BillingPurchase, digest(TOKEN))
            assert api.m.service.utc(canceled.checked_at) == NOW
            assert api.m.service.utc(canceled.expires_at) == expiry
        original(*args)

    monkeypatch.setattr(api.play, "acknowledge", acknowledge)
    response = verify_http(api)
    assert response.status_code == 200, response.text
    plan = response.json()["entitlement"]
    assert plan["ad_free"] and plan["expires_at"] == stamp(expiry)
    assert plan["subscription_state"] == "CANCELED"
    assert plan["auto_renewing"] is False and plan["next_billing_at"] is None
    assert len(api.play.ack_calls) == 1


@pytest.mark.parametrize("state,auto_renewing", [("CANCELED", False), ("IN_GRACE_PERIOD", True)])
def test_paid_canceled_and_grace_period_plans_do_not_claim_a_next_charge(api, state, auto_renewing):
    api.play.receipts[TOKEN] = subscription(acknowledged=True, state=state)
    plan = verify_http(api).json()["entitlement"]
    assert plan["ad_free"] and plan["subscription_state"] == state
    assert plan["auto_renewing"] is auto_renewing and plan["next_billing_at"] is None
    assert plan["expires_at"] == stamp(NOW + timedelta(days=30))


def test_get_plan_refreshes_google_cancel_even_inside_a_valid_lease(api):
    api.play.receipts[TOKEN] = subscription(acknowledged=True)
    initial = verify_http(api).json()["entitlement"]
    api.clock.now = NOW + timedelta(seconds=1)
    api.play.receipts[TOKEN] = subscription(acknowledged=True, state="CANCELED")
    response = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A])
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["ad_free"] and plan["subscription_state"] == "CANCELED"
    assert plan["auto_renewing"] is False and plan["next_billing_at"] is None
    assert plan["expires_at"] == initial["expires_at"]
    assert len(api.play.calls) == 2


@pytest.mark.parametrize("error", ["invalid_purchase", "not_found"])
def test_invalid_google_plan_refresh_does_not_reuse_a_paid_display_lease(api, error):
    api.play.receipts[TOKEN] = subscription(acknowledged=True)
    assert verify_http(api).json()["entitlement"]["ad_free"]
    api.play.receipts[TOKEN] = api.m.adapter.PlayError(error)
    response = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A])
    assert response.status_code == 200, response.text
    plan = response.json()
    assert not plan["ad_free"] and plan["product_id"] is None
    assert plan["subscription_state"] is None and plan["next_billing_at"] is None
    with api.factory() as db:
        assert not db.get(api.m.models.BillingPurchase, digest(TOKEN)).active


def test_current_plan_refresh_reselects_another_still_paid_subscription(api):
    monthly_token = TOKEN + "-monthly"
    api.play.receipts[monthly_token] = subscription(acknowledged=True)
    assert verify_http(api, token=monthly_token).json()["entitlement"]["product_id"] == MONTHLY
    api.play.receipts[TOKEN] = subscription(acknowledged=True, product=YEARLY, expiry=NOW + timedelta(days=365))
    assert verify_http(api, product=YEARLY).json()["entitlement"]["product_id"] == YEARLY
    api.play.receipts[TOKEN] = subscription(acknowledged=True, product=YEARLY, state="ON_HOLD",
                                           expiry=NOW + timedelta(days=365))
    plan = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A]).json()
    assert plan["ad_free"] and plan["product_id"] == MONTHLY
    assert plan["subscription_state"] == "ACTIVE"
    assert plan["next_billing_at"] == stamp(NOW + timedelta(days=30))


def test_lifetime_plan_has_no_renewal_fields_or_optional_google_query(api):
    api.play.receipts[TOKEN] = lifetime(acknowledged=True)
    plan = verify_http(api, product=LIFETIME, kind="inapp").json()["entitlement"]
    assert plan["ad_free"] and plan["product_id"] == LIFETIME
    for field in ("expires_at", "next_billing_at", "auto_renewing", "subscription_state"):
        assert plan[field] is None
    calls = len(api.play.calls)
    current = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A]).json()
    assert current["ad_free"] and current["next_billing_at"] is None
    assert len(api.play.calls) == calls


def test_optional_plan_fetch_cannot_grant_a_lease_that_expired_while_waiting(api, monkeypatch):
    api.play.receipts[TOKEN] = subscription(acknowledged=True, expiry=NOW + timedelta(seconds=5))
    assert verify_http(api).json()["entitlement"]["ad_free"]

    def outage_after_expiry(token):
        api.clock.now = NOW + timedelta(seconds=6)
        raise api.m.adapter.PlayError("unavailable")

    monkeypatch.setattr(api.play, "get_subscription", outage_after_expiry)
    response = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A])
    assert response.status_code == 200, response.text
    assert not response.json()["ad_free"] and response.json()["next_billing_at"] is None


def test_expiry_boundary_rechecks_google_and_denies_genuinely_expired_purchase(api):
    expiry = NOW + timedelta(seconds=30)
    api.play.receipts[TOKEN] = subscription(acknowledged=True, expiry=expiry)
    assert verify_http(api).json()["entitlement"]["ad_free"]
    api.play.receipts[TOKEN] = subscription(state="EXPIRED", acknowledged=True, expiry=expiry)
    api.clock.now = expiry
    response = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A])
    assert response.status_code == 200 and not response.json()["ad_free"]
    assert len(api.play.calls) == 2


def test_google_revocation_overrides_unexpired_local_lease(api):
    api.play.receipts[TOKEN] = subscription(acknowledged=True)
    assert verify_http(api).json()["entitlement"]["ad_free"]
    api.play.receipts[TOKEN] = subscription(state="EXPIRED", acknowledged=True, expiry=NOW - timedelta(seconds=1))
    with api.factory() as db:
        billing = service(api, db)
        billing.reconcile_notification(TOKEN, "subs")
        assert not billing.entitlement(USER_A)["ad_free"]


def test_lifetime_void_is_terminal_even_if_a_receipt_is_replayed(api):
    api.play.receipts[TOKEN] = lifetime()
    assert verify_http(api, product=LIFETIME, kind="inapp").json()["entitlement"]["ad_free"]
    api.play.receipts[TOKEN] = lifetime(state="CANCELLED", acknowledged=True)
    with api.factory() as db:
        billing = service(api, db)
        billing.handle_voided({"purchaseToken": TOKEN, "orderId": "GPA.synthetic", "voidedQuantity": 1})
        assert not billing.entitlement(USER_A)["ad_free"]
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)).revoked_at is not None
    api.play.receipts[TOKEN] = lifetime(acknowledged=True)
    replay = verify_http(api, product=LIFETIME, kind="inapp")
    assert replay.status_code == 200 and not replay.json()["entitlement"]["ad_free"]


def test_google_zero_refundable_quantity_revokes_on_refresh_and_prevents_resurrection(api):
    api.play.receipts[TOKEN] = lifetime(acknowledged=True)
    assert verify_http(api, product=LIFETIME, kind="inapp").json()["entitlement"]["ad_free"]
    api.play.receipts[TOKEN]["productLineItem"][0]["productOfferDetails"]["refundableQuantity"] = 0
    api.clock.now = NOW + timedelta(minutes=5)
    response = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A])
    assert response.status_code == 200 and not response.json()["ad_free"]
    with api.factory() as db:
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)).revoked_at is not None
    api.play.receipts[TOKEN] = lifetime(acknowledged=True)
    response = verify_http(api, product=LIFETIME, kind="inapp")
    assert response.status_code == 200 and not response.json()["entitlement"]["ad_free"]


def test_refund_of_old_subscription_renewal_rechecks_current_entitlement(api):
    api.play.receipts[TOKEN] = subscription(acknowledged=True)
    assert verify_http(api).json()["entitlement"]["ad_free"]
    before = len(api.play.calls)
    with api.factory() as db:
        billing = service(api, db)
        billing.handle_voided({"purchaseToken": TOKEN, "orderId": "GPA.synthetic-prior-renewal.0"})
        assert billing.entitlement(USER_A)["ad_free"]
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)).revoked_at is None
    assert len(api.play.calls) == before + 1


def test_cipher_rotation_keeps_old_tokens_readable_and_errors_safe(api):
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    old = api.m.service.TokenCipher(old_key).encrypt(TOKEN)
    rotated = api.m.service.TokenCipher([new_key, old_key])
    assert rotated.decrypt(old) == TOKEN
    assert Fernet(new_key).decrypt(rotated.encrypt(TOKEN).encode()).decode() == TOKEN
    with pytest.raises(api.m.service.BillingError, match="^billing_unavailable$"):
        rotated.decrypt("corrupt-token-ciphertext")


def test_ownership_tombstone_survives_user_deletion(api):
    api.play.receipts[TOKEN] = subscription(acknowledged=True)
    assert verify_http(api).status_code == 200
    with api.factory() as db:
        db.delete(db.get(api.m.User, USER_A))
        db.commit()
    assert api.client.get("/api/billing/entitlement", headers=api.headers[USER_A]).status_code == 401
    api.play.receipts[TOKEN] = subscription(USER_B)
    assert verify_http(api, user=USER_B).status_code == 409


@pytest.mark.parametrize("path,method", [("/config", "get"), ("/entitlement", "get"),
                                       ("/google-play/verify", "post")])
def test_android_routes_require_current_database_backed_jwt(api, path, method):
    request = getattr(api.client, method)
    kwargs = {"json": verify_body()} if method == "post" else {}
    assert request("/api/billing" + path, **kwargs).status_code == 401
    assert request("/api/billing" + path, headers={"Authorization": "Bearer invalid-jwt"}, **kwargs).status_code == 401
    with api.factory() as db:
        db.get(api.m.User, USER_A).is_active = False
        db.commit()
    assert request("/api/billing" + path, headers=api.headers[USER_A], **kwargs).status_code == 401


def test_default_disabled_sales_never_query_google(api, monkeypatch):
    for key in ("GOOGLE_PLAY_BILLING_ENABLED", "GOOGLE_PLAY_SERVICE_ACCOUNT_FILE", "GOOGLE_PLAY_TOKEN_KEY_FILE",
                "GOOGLE_PLAY_RTDN_AUDIENCE", "GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL",
                "GOOGLE_PLAY_RTDN_EMAIL", "GOOGLE_PLAY_RTDN_SUBSCRIPTION"):
        monkeypatch.delenv(key, raising=False)
    default = api.m.adapter.BillingSettings.from_env()
    assert not default.enabled and not default.configuration_complete
    api.settings = default
    config = api.client.get("/api/billing/config", headers=api.headers[USER_A])
    assert config.status_code == 200
    assert config.json() == {"api_version": 1, "google_play_ready": False, "product_ids": [MONTHLY, YEARLY, LIFETIME]}
    assert verify_http(api).status_code == 503
    assert api.play.calls == []


@pytest.mark.parametrize("extra", [{"user_id": USER_B}, {"verified": True}, {"acknowledged": True}])
def test_verify_rejects_client_account_or_entitlement_claims_without_echo(api, extra):
    response = api.client.post("/api/billing/google-play/verify", json=verify_body(**extra), headers=api.headers[USER_A])
    assert response.status_code == 400 and TOKEN not in response.text
    assert api.play.calls == []


@pytest.mark.parametrize("body", [[], {"purchase_token": TOKEN}, verify_body(product="unknown"),
                                 verify_body(kind="inapp"), verify_body(token="short")])
def test_verify_validation_never_echoes_receipt(api, body, caplog):
    response = api.client.post("/api/billing/google-play/verify", json=body, headers=api.headers[USER_A])
    assert response.status_code == 400
    assert TOKEN not in response.text and TOKEN not in caplog.text


def envelope(api, *, message_id="fixture-message-1", kind="subscriptionNotification", token=TOKEN):
    event = {"version": "1.0"} if kind == "testNotification" else {"purchaseToken": token, "notificationType": 4}
    data = {"version": "1.0", "packageName": "com.weheome.app", "eventTimeMillis": str(int(NOW.timestamp() * 1000)), kind: event}
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return {"subscription": api.settings.rtdn_subscription, "message": {"messageId": message_id, "data": encoded}}


def push_auth(api, monkeypatch, *, email=None, verified=True, fail=False):
    oidc = importlib.import_module("google.oauth2.id_token")
    calls = []

    def verify(token, request, audience):
        calls.append((token, audience))
        assert token == PUSH_BEARER[7:]
        assert audience == api.settings.rtdn_audience
        if fail:
            raise ValueError("synthetic audience/signature failure")
        return {"email": email or api.settings.rtdn_email, "email_verified": verified}

    monkeypatch.setattr(oidc, "verify_oauth2_token", verify)
    return calls


@pytest.mark.parametrize("failure", ["missing", "bad_signature_or_audience", "wrong_email", "email_not_verified"])
def test_rtdn_requires_verified_google_push_identity(api, monkeypatch, failure):
    push_auth(api, monkeypatch, email="other@offline-fixture.iam.gserviceaccount.com" if failure == "wrong_email" else None,
              verified=failure != "email_not_verified", fail=failure == "bad_signature_or_audience")
    headers = {} if failure == "missing" else {"Authorization": PUSH_BEARER}
    response = api.client.post("/api/billing/google-play/rtdn", json=envelope(api), headers=headers)
    assert response.status_code == 401 and TOKEN not in response.text
    assert api.play.calls == []
    with api.factory() as db:
        assert db.query(api.m.models.BillingNotification).count() == 0


@pytest.mark.parametrize("damage", ["subscription", "package", "base64", "multiple_kinds"])
def test_rtdn_validates_subscription_package_and_envelope(api, monkeypatch, damage):
    push_auth(api, monkeypatch)
    body = envelope(api)
    if damage == "subscription":
        body["subscription"] = "projects/other/subscriptions/other"
    elif damage == "base64":
        body["message"]["data"] = "!invalid-base64!"
    else:
        data = json.loads(base64.b64decode(body["message"]["data"]))
        if damage == "package":
            data["packageName"] = "com.other.app"
        else:
            data["testNotification"] = {}
        body["message"]["data"] = base64.b64encode(json.dumps(data).encode()).decode()
    response = api.client.post("/api/billing/google-play/rtdn", json=body, headers={"Authorization": PUSH_BEARER})
    assert response.status_code == 400 and api.play.calls == []


def test_rtdn_queries_google_before_dedup_mark_and_retries_failure(api, monkeypatch):
    auth_calls = push_auth(api, monkeypatch)
    assert api.client.get("/api/billing/config", headers=api.headers[USER_A]).json()["google_play_ready"]
    body = envelope(api)
    api.play.receipts[TOKEN] = api.m.adapter.PlayError("unavailable")
    response = api.client.post("/api/billing/google-play/rtdn", json=body, headers={"Authorization": PUSH_BEARER})
    assert response.status_code == 503
    with api.factory() as db:
        assert db.get(api.m.models.BillingNotification, "fixture-message-1") is None
    api.play.receipts[TOKEN] = subscription()
    assert api.client.post("/api/billing/google-play/rtdn", json=body, headers={"Authorization": PUSH_BEARER}).status_code == 204
    # Failed initial lookup, successful account discovery, then current state under its lock.
    assert len(api.play.calls) == 3 and len(api.play.ack_calls) == 1
    with api.factory() as db:
        assert db.get(api.m.models.BillingNotification, "fixture-message-1") is not None
        assert service(api, db).entitlement(USER_A)["ad_free"]
    calls = len(api.play.calls)  # The separate entitlement request also reads current plan display data.
    assert api.client.post("/api/billing/google-play/rtdn", json=body, headers={"Authorization": PUSH_BEARER}).status_code == 204
    assert len(api.play.calls) == calls and len(auth_calls) == 3


def test_rtdn_test_notification_works_while_checkout_disabled(api, monkeypatch):
    push_auth(api, monkeypatch)
    api.settings = replace(api.settings, enabled=False)
    body = envelope(api, kind="testNotification")
    response = api.client.post("/api/billing/google-play/rtdn", json=body, headers={"Authorization": PUSH_BEARER})
    assert response.status_code == 204 and api.play.calls == []


def test_rtdn_lifetime_void_retries_google_lag_before_dedup(api, monkeypatch):
    push_auth(api, monkeypatch)
    api.play.receipts[TOKEN] = lifetime(acknowledged=True)
    assert verify_http(api, product=LIFETIME, kind="inapp").status_code == 200
    body = envelope(api, kind="voidedPurchaseNotification")
    headers = {"Authorization": PUSH_BEARER}
    response = api.client.post("/api/billing/google-play/rtdn", json=body, headers=headers)
    assert response.status_code == 503 and response.json()["detail"] == "purchase_revocation_pending"
    with api.factory() as db:
        assert db.get(api.m.models.BillingNotification, "fixture-message-1") is None
        assert service(api, db).entitlement(USER_A)["ad_free"]
    api.play.receipts[TOKEN] = lifetime(state="CANCELLED", acknowledged=True)
    assert api.client.post("/api/billing/google-play/rtdn", json=body, headers=headers).status_code == 204
    with api.factory() as db:
        assert db.get(api.m.models.BillingNotification, "fixture-message-1") is not None
        assert not service(api, db).entitlement(USER_A)["ad_free"]
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)).revoked_at is not None


def test_deleted_account_notification_cannot_acknowledge_receipt(api):
    api.play.receipts[TOKEN] = subscription()
    api.play.ack_error = True
    assert verify_http(api).status_code == 503
    with api.factory() as db:
        db.get(api.m.User, USER_A).is_active = False
        db.commit()
        api.play.ack_error = False
        before = len(api.play.calls), len(api.play.ack_calls)
        service(api, db).reconcile_notification(TOKEN, "subs")
        assert (len(api.play.calls), len(api.play.ack_calls)) == before


def test_voided_sync_paginates_and_advances_cursor_only_after_complete_success(api):
    api.play.receipts[TOKEN] = lifetime()
    assert verify_http(api, product=LIFETIME, kind="inapp").status_code == 200
    api.play.pages = {
        None: {"voidedPurchases": [{"purchaseToken": TOKEN, "orderId": "GPA.synthetic"}],
               "tokenPagination": {"nextPageToken": "next-fixture-page"}},
        "next-fixture-page": api.m.adapter.PlayError("unavailable"),
    }
    with api.factory() as db:
        with pytest.raises(api.m.adapter.PlayError):
            service(api, db).sync_voided()
        assert db.get(api.m.models.BillingSyncState, "voided") is None
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)).revoked_at is not None
        api.play.pages["next-fixture-page"] = {"voidedPurchases": []}
        assert service(api, db).sync_voided() == 1
        assert api.m.service.utc(db.get(api.m.models.BillingSyncState, "voided").checked_until) == NOW
    voided_calls = [call for call in api.play.calls if call[0] == "voided"]
    assert [call[-1] for call in voided_calls] == [None, "next-fixture-page", None, "next-fixture-page"]
    assert voided_calls[0][1] == int((NOW - timedelta(days=30)).timestamp() * 1000)


def operator_module():
    return importlib.import_module("app.billing_ops")


def operator_main(api, monkeypatch, settings, capsys):
    ops = operator_module()
    monkeypatch.setattr(sys, "argv", ["billing_ops", "check"])
    monkeypatch.setattr(api.m.adapter.BillingSettings, "from_env", classmethod(lambda cls: settings))
    code = 0
    try:
        ops.main()
    except SystemExit as error:
        code = error.code
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def test_operator_missing_environment_stops_before_database_google_and_key_read(api, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must not open database, Google or secret files")

    database = importlib.import_module("app.core.database")
    monkeypatch.setattr(database, "SessionLocal", forbidden)
    monkeypatch.setattr(api.m.adapter, "PlayClient", forbidden)
    monkeypatch.setattr(api.m.service.TokenCipher, "from_file", forbidden)
    code, result = operator_main(api, monkeypatch, api.m.adapter.BillingSettings(), capsys)
    assert code == 1 and result["stage"] == "configuration"
    assert result["reason"] == "missing_environment" and result["checkout_enabled"] is False
    assert result["missing_env"] == [name for name, _ in operator_module()._SETTINGS]
    assert result["invalid_fields"] == []


@pytest.mark.parametrize("damage,expected", [
    ("relative", "absolute_path_required"), ("missing", "file_missing"),
    ("directory", "regular_file_required"), ("unreadable", "file_unreadable"),
    ("metadata_error", "file_metadata_unavailable"),
    ("email", "service_account_email_required"), ("subscription", "subscription_resource_required"),
])
def test_operator_invalid_metadata_reports_only_setting_names(api, monkeypatch, tmp_path, damage, expected):
    from pathlib import Path
    ops = operator_module()
    private = "private-value-never-print-operator-fixture"
    name = "GOOGLE_PLAY_SERVICE_ACCOUNT_FILE"
    settings = api.settings
    if damage == "relative":
        settings = replace(settings, credentials_file=private + ".json")
    elif damage == "missing":
        settings = replace(settings, credentials_file=str(tmp_path / private))
    elif damage == "directory":
        settings = replace(settings, credentials_file=str(tmp_path))
    elif damage == "unreadable":
        original = ops.os.access
        monkeypatch.setattr(ops.os, "access", lambda path, mode: False
                            if str(path) == settings.credentials_file else original(path, mode))
    elif damage == "metadata_error":
        original = Path.exists
        def denied(path):
            if str(path) == settings.credentials_file:
                raise OSError(private + " SQL/mysql/private-path")
            return original(path)
        monkeypatch.setattr(Path, "exists", denied)
    elif damage == "email":
        name = "GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL"
        settings = replace(settings, rtdn_email=private)
    else:
        name = "GOOGLE_PLAY_RTDN_SUBSCRIPTION"
        settings = replace(settings, rtdn_subscription=private)
    missing, invalid = ops.configuration_issues(settings)
    assert not missing and invalid == [{"name": name, "reason": expected}]
    serialized = json.dumps(invalid)
    assert private not in serialized and settings.credentials_file not in serialized


@pytest.mark.parametrize("canonical_present,missing", [(False, False), (True, True)])
def test_operator_email_alias_uses_existing_environment_precedence(api, monkeypatch, canonical_present, missing):
    ops = operator_module()
    for name, field in ops._SETTINGS:
        monkeypatch.setenv(name, getattr(api.settings, field))
    monkeypatch.setenv("GOOGLE_PLAY_BILLING_ENABLED", "false")
    monkeypatch.setenv("GOOGLE_PLAY_RTDN_EMAIL", api.settings.rtdn_email)
    if canonical_present:
        monkeypatch.setenv("GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL", "")
    else:
        monkeypatch.delenv("GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL")
    settings = api.m.adapter.BillingSettings.from_env()
    names, invalid = ops.configuration_issues(settings)
    assert ("GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL" in names) is missing
    assert not invalid and settings.enabled is False


def test_operator_disabled_checkout_can_pass_readonly_preflight_without_paid_state(api, monkeypatch, capsys):
    calls = []
    def client(settings):
        assert settings.enabled is False
        calls.append("client")
        return SimpleNamespace(catalog_ready=lambda: True, close=lambda: calls.append("closed"))
    monkeypatch.setattr(api.m.adapter, "PlayClient", client)
    settings = replace(api.settings, enabled=False)
    code, result = operator_main(api, monkeypatch, settings, capsys)
    assert code == 0 and result == {"ok": True, "configuration_validated": True, "checkout_enabled": False}
    assert calls == ["client", "closed"] and settings.enabled is False
    with api.factory() as db:
        assert db.query(api.m.models.BillingPurchase).count() == 0
        assert db.query(api.m.models.BillingAccount).count() == 0
        assert db.query(api.m.models.BillingNotification).count() == 0
    api.settings = settings
    assert not api.client.get("/api/billing/config", headers=api.headers[USER_A]).json()["google_play_ready"]
    assert verify_http(api).status_code == 503


@pytest.mark.parametrize("damage,stage,reason", [
    ("database", "database", "database_unavailable"),
    ("schema_error", "schema", "billing_schema_check_failed"),
    ("missing_table", "schema", "billing_tables_missing"),
    ("token_key", "token_key", "token_encryption_key_invalid"),
    ("catalog_exception", "catalog", "google_catalog_check_failed"),
    ("catalog_false", "catalog", "google_catalog_unavailable_or_mismatch"),
])
def test_operator_failure_stage_hides_exception_paths_sql_and_secrets(api, monkeypatch, capsys,
                                                                  damage, stage, reason):
    import logging
    import sqlalchemy
    from pathlib import Path
    private = "PRIVATE_SQL_TOKEN_SECRET_NEVER_PRINT"
    database = importlib.import_module("app.core.database")
    def fail(*args, **kwargs):
        logging.getLogger("sqlalchemy.engine").critical(private)
        raise RuntimeError("mysql://private-user:" + private + "@private-host/database")
    monkeypatch.setattr(api.m.adapter, "PlayClient",
                        lambda settings: SimpleNamespace(catalog_ready=lambda: True, close=lambda: None))
    if damage == "database":
        monkeypatch.setattr(database, "SessionLocal", fail)
    elif damage == "schema_error":
        monkeypatch.setattr(sqlalchemy, "inspect", fail)
    elif damage == "missing_table":
        api.m.models.BillingPurchase.__table__.drop(api.engine)
    elif damage == "token_key":
        Path(api.settings.token_key_file).write_text(private)
    else:
        monkeypatch.setattr(api.m.adapter, "PlayClient", lambda settings: SimpleNamespace(
            catalog_ready=fail if damage == "catalog_exception" else (lambda: False), close=lambda: None))
    before = logging.root.manager.disable
    code, result = operator_main(api, monkeypatch, replace(api.settings, enabled=False), capsys)
    assert logging.root.manager.disable == before
    assert code == 1 and result["stage"] == stage and result["reason"] == reason
    assert result["checkout_enabled"] is False
    output = json.dumps(result)
    assert private not in output and "mysql://" not in output
    assert api.settings.credentials_file not in output and api.settings.token_key_file not in output
    if damage == "missing_table":
        assert result["missing_tables"] == ["billing_purchases"]


def test_operator_logging_restores_stricter_existing_threshold(api, monkeypatch):
    import logging
    ops = operator_module()
    monkeypatch.setattr(api.m.adapter, "PlayClient", lambda settings: SimpleNamespace(
        catalog_ready=lambda: False, close=lambda: None))
    original = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL + 20)
        result = ops.check_setup(replace(api.settings, enabled=False))
        assert not result["ok"] and logging.root.manager.disable == logging.CRITICAL + 20
    finally:
        logging.disable(original)


def delete_fixture_account(api):
    helper = importlib.import_module("app.services.billing_account_deletion")
    with api.factory() as db:
        user = db.query(api.m.User).filter_by(user_id=USER_A).with_for_update().first()
        helper.delete_billing_account_data(db, USER_A)
        db.delete(user)
        db.commit()


def test_deletion_during_google_fetch_cannot_recreate_purchase(api, monkeypatch):
    api.play.receipts[TOKEN] = subscription()
    original = api.play.get_subscription

    def fetch(token):
        payload = original(token)
        delete_fixture_account(api)
        return payload

    monkeypatch.setattr(api.play, "get_subscription", fetch)
    response = verify_http(api)
    assert response.status_code == 409
    assert not api.play.ack_calls
    with api.factory() as db:
        assert db.get(api.m.models.BillingAccount, USER_A) is None
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)) is None


def test_deletion_during_optional_plan_fetch_cannot_reactivate_an_erased_receipt(api, monkeypatch):
    api.play.receipts[TOKEN] = subscription(acknowledged=True)
    assert verify_http(api).json()["entitlement"]["ad_free"]
    original = api.play.get_subscription

    def fetch(token):
        payload = original(token)
        delete_fixture_account(api)
        return payload

    monkeypatch.setattr(api.play, "get_subscription", fetch)
    response = api.client.get("/api/billing/entitlement", headers=api.headers[USER_A])
    assert response.status_code == 409
    with api.factory() as db:
        record = db.get(api.m.models.BillingPurchase, digest(TOKEN))
        assert record.user_id == "__deleted_billing_owner__" and not record.active
        assert record.product_id is None and record.token_encrypted == ""
        assert record.checked_at is None and db.get(api.m.models.BillingAccount, USER_A) is None


@pytest.mark.parametrize("ack_failure", [False, True])
def test_deletion_during_google_ack_cannot_restore_erased_data(api, monkeypatch, ack_failure):
    api.play.receipts[TOKEN] = subscription()
    original = api.play.acknowledge

    def acknowledge(*args):
        delete_fixture_account(api)
        if ack_failure:
            raise api.m.adapter.PlayError("unavailable")
        original(*args)

    monkeypatch.setattr(api.play, "acknowledge", acknowledge)
    assert verify_http(api).status_code == 409
    with api.factory() as db:
        record = db.get(api.m.models.BillingPurchase, digest(TOKEN))
        assert record.user_id == "__deleted_billing_owner__"
        assert record.token_encrypted == "" and record.product_id is None
        assert not record.active and not record.acknowledged
        assert record.checked_at is None and record.revoked_at is None
        assert db.get(api.m.models.BillingAccount, USER_A) is None


def test_voided_sync_skips_erased_tokens_and_advances_cursor(api):
    api.play.receipts[TOKEN] = lifetime(acknowledged=True)
    assert verify_http(api, product=LIFETIME, kind="inapp").status_code == 200
    delete_fixture_account(api)
    api.play.pages[None] = {"voidedPurchases": [{"purchaseToken": TOKEN}]}
    before = len(api.play.calls)
    with api.factory() as db:
        assert service(api, db).sync_voided() == 1
        record = db.get(api.m.models.BillingPurchase, digest(TOKEN))
        assert record.checked_at is None and record.revoked_at is None
        assert db.get(api.m.models.BillingSyncState, "voided") is not None
    assert len(api.play.calls) == before + 1  # Only authoritative list, no decrypt/receipt lookup.


def test_ack_retry_isolates_failure_and_recovers_next_receipt(api):
    other = TOKEN + "-other"
    api.play.receipts[TOKEN] = subscription()
    api.play.receipts[other] = subscription(USER_B)
    api.play.ack_error = True
    assert verify_http(api).status_code == 503
    assert verify_http(api, user=USER_B, token=other).status_code == 503
    api.play.ack_error = False
    api.play.receipts[TOKEN] = api.m.adapter.PlayError("unavailable")
    with api.factory() as db:
        assert service(api, db).retry_unacknowledged() == {"checked": 1, "failed": 1}
        assert db.get(api.m.models.BillingPurchase, digest(other)).acknowledged
        assert not db.get(api.m.models.BillingPurchase, digest(TOKEN)).active


def test_ack_retry_bounded_failure_does_not_starve_next_receipt(api):
    other = TOKEN + "-other"
    api.play.receipts[TOKEN] = subscription()
    api.play.receipts[other] = subscription(USER_B)
    api.play.ack_error = True
    assert verify_http(api).status_code == 503
    assert verify_http(api, user=USER_B, token=other).status_code == 503
    first, second = sorted([TOKEN, other], key=digest)
    api.play.ack_error = False
    api.play.receipts[first] = api.m.adapter.PlayError("unavailable")
    with api.factory() as db:
        billing = service(api, db)
        assert billing.retry_unacknowledged(limit=1) == {"checked": 0, "failed": 1}
        assert billing.retry_unacknowledged(limit=1) == {"checked": 1, "failed": 0}
        assert db.get(api.m.models.BillingPurchase, digest(second)).acknowledged


def test_pilot_allows_only_authenticated_selected_account_with_global_sales_off(api):
    api.settings = replace(api.settings, enabled=False, test_user_ids=(USER_A,))
    assert api.client.get("/api/billing/config", headers=api.headers[USER_A]).json()["google_play_ready"]
    assert not api.client.get("/api/billing/config", headers=api.headers[USER_B]).json()["google_play_ready"]
    api.play.receipts[TOKEN] = subscription()
    assert verify_http(api, user=USER_B).status_code == 503
    assert not api.play.calls and not api.play.ack_calls
    assert verify_http(api).json()["entitlement"]["ad_free"]


@pytest.mark.parametrize("raw", ["not-a-uuid", USER_A + ",broken", USER_A + ",", ""])
def test_malformed_pilot_allowlist_never_opens_sales(isolated_modules, monkeypatch, raw):
    monkeypatch.setenv("GOOGLE_PLAY_BILLING_ENABLED", "false")
    monkeypatch.setenv("GOOGLE_PLAY_BILLING_TEST_USER_IDS", raw)
    settings = isolated_modules.adapter.BillingSettings.from_env()
    assert not settings.checkout_enabled_for(USER_A)
    assert not settings.maintenance_enabled


def test_pilot_allowlist_parses_trimmed_canonical_ids(isolated_modules, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_BILLING_ENABLED", "false")
    monkeypatch.setenv("GOOGLE_PLAY_BILLING_TEST_USER_IDS", " " + USER_A + ", " + USER_B + " ")
    settings = isolated_modules.adapter.BillingSettings.from_env()
    assert settings.test_user_ids == (USER_A, USER_B)
    assert settings.checkout_enabled_for(USER_A) and settings.maintenance_enabled
    assert not settings.checkout_enabled_for(None)


def test_invalid_pilot_configuration_has_safe_operator_diagnostic(isolated_modules, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_BILLING_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_PLAY_BILLING_TEST_USER_IDS", "not-a-uuid")
    settings = isolated_modules.adapter.BillingSettings.from_env()
    assert not settings.configuration_complete and not settings.checkout_enabled_for(USER_A)
    ops = importlib.import_module("app.billing_ops")
    result = ops.check_setup(settings)
    assert {"name": "GOOGLE_PLAY_BILLING_TEST_USER_IDS", "reason": "invalid_uuid_list"} in result["invalid_fields"]
    assert "not-a-uuid" not in json.dumps(result)


def test_new_notification_rechecks_current_state_after_account_discovery(api, monkeypatch):
    with api.factory() as db:
        service(api, db).register_account(USER_A)
    api.play.receipts[TOKEN] = subscription()
    original = api.play.get_subscription
    calls = []

    def fetch(token):
        payload = original(token)
        calls.append(token)
        # State changed while account discovery was in flight. The first
        # response cannot be used to deliver access or acknowledge the receipt.
        api.play.receipts[token] = subscription(state="EXPIRED", expiry=NOW - timedelta(seconds=1))
        return payload

    monkeypatch.setattr(api.play, "get_subscription", fetch)
    with api.factory() as db:
        billing = service(api, db)
        billing.reconcile_notification(TOKEN, "subs")
        assert not billing.entitlement(USER_A)["ad_free"]
    assert len(calls) == 2 and not api.play.ack_calls
