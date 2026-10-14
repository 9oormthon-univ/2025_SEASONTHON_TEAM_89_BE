"""Google Play transport and fail-closed parsing for the fixed ad-free catalog.

No credential is read and no Google request is made at import time. The transport
and credentials can be supplied by offline tests. Purchase tokens must never be
included in errors or logs from this module.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

import httpx


PACKAGE_NAME = "com.weheome.app"
ANDROIDPUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
API_ROOT = "https://androidpublisher.googleapis.com/androidpublisher/v3"
REQUEST_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class ProductSpec:
    product_type: str
    krw_price: int
    base_plan_id: Optional[str] = None
    billing_period: Optional[str] = None
    purchase_option_id: Optional[str] = None


PRODUCTS: Mapping[str, ProductSpec] = {
    "ad_free_monthly": ProductSpec("subs", 990, "monthly", "P1M"),
    "ad_free_yearly": ProductSpec("subs", 9900, "yearly", "P1Y"),
    "ad_free_lifetime": ProductSpec("inapp", 20000, purchase_option_id="buy"),
}


@dataclass(frozen=True)
class BillingSettings:
    enabled: bool = False
    credentials_file: str = field(default="", repr=False)
    token_key_file: str = field(default="", repr=False)
    rtdn_audience: str = ""
    rtdn_email: str = ""
    rtdn_subscription: str = ""
    test_user_ids: tuple[str, ...] = field(default=(), repr=False)
    test_users_valid: bool = field(default=True, repr=False)

    def checkout_enabled_for(self, user_id: str | None) -> bool:
        return self.test_users_valid and (self.enabled or bool(user_id and user_id in self.test_user_ids))

    @property
    def maintenance_enabled(self) -> bool:
        return self.test_users_valid and (self.enabled or bool(self.test_user_ids))

    @classmethod
    def from_env(cls) -> "BillingSettings":
        from uuid import UUID
        raw_test_users = os.getenv("GOOGLE_PLAY_BILLING_TEST_USER_IDS", "").strip()
        test_users = tuple(item.strip() for item in raw_test_users.split(",")) if raw_test_users else ()
        test_users_valid = True
        try:
            if any(str(UUID(item)) != item for item in test_users):
                raise ValueError()
        except (ValueError, AttributeError):
            test_users = ()  # Malformed allowlists never open checkout.
            test_users_valid = False
        return cls(
            enabled=os.getenv("GOOGLE_PLAY_BILLING_ENABLED", "false").strip().lower()
            in {"true", "1", "yes", "on"},
            credentials_file=os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_FILE", "").strip(),
            token_key_file=os.getenv("GOOGLE_PLAY_TOKEN_KEY_FILE", "").strip(),
            rtdn_audience=os.getenv("GOOGLE_PLAY_RTDN_AUDIENCE", "").strip(),
            rtdn_email=os.getenv(
                "GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL",
                os.getenv("GOOGLE_PLAY_RTDN_EMAIL", ""),
            ).strip(),
            rtdn_subscription=os.getenv("GOOGLE_PLAY_RTDN_SUBSCRIPTION", "").strip(),
            test_user_ids=tuple(dict.fromkeys(test_users)),
            test_users_valid=test_users_valid,
        )

    @property
    def configuration_complete(self) -> bool:
        """Check local configuration without opening keys or contacting Google."""
        if not self.test_users_valid:
            return False
        try:
            paths_ready = all(
                value
                and Path(value).is_absolute()
                and Path(value).is_file()
                and os.access(value, os.R_OK)
                for value in (self.credentials_file, self.token_key_file)
            )
        except (OSError, ValueError):
            return False
        return bool(
            paths_ready
            and self.rtdn_audience
            and self.rtdn_email.endswith(".gserviceaccount.com")
            and "@" in self.rtdn_email
            and re.fullmatch(r"projects/[^/\s]+/subscriptions/[^/\s]+", self.rtdn_subscription)
        )


class PlayError(Exception):
    """Safe error codes: never preserve response bodies, URLs or tokens."""

    def __init__(self, code: str = "unavailable") -> None:
        self.code = code if code in {"invalid_purchase", "unavailable", "not_found"} else "unavailable"
        super().__init__(self.code)


@dataclass(frozen=True)
class PurchaseSnapshot:
    product_id: str
    product_type: str
    account_id: str = field(repr=False)
    active: bool
    completed: bool
    acknowledged: bool
    expires_at: Optional[datetime] = None
    linked_token: Optional[str] = field(default=None, repr=False)
    order_id: Optional[str] = field(default=None, repr=False)
    revoked: bool = False
    subscription_state: Optional[str] = None
    auto_renewing: Optional[bool] = None
    next_billing_at: Optional[datetime] = None


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise PlayError("invalid_purchase")
    return value


def _one_item(value: Any) -> dict:
    if not isinstance(value, list) or len(value) != 1:
        raise PlayError("invalid_purchase")
    return _object(value[0])


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise PlayError("invalid_purchase")
    return value


def _account(value: Any) -> str:
    # The Android client supplies the lowercase SHA-256 hex of its user ID.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PlayError("invalid_purchase")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise PlayError("invalid_purchase")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise PlayError("invalid_purchase") from None


def _acknowledged(payload: dict) -> bool:
    state = payload.get("acknowledgementState")
    if not isinstance(state, str) or state not in {
        "ACKNOWLEDGEMENT_STATE_PENDING", "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"
    }:
        raise PlayError("invalid_purchase")
    return state == "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"


def snapshot(
    payload: dict, product_id: str, product_type: str, now: datetime
) -> PurchaseSnapshot:
    """Interpret only the catalog this app sells; unknown/malformed data denies.

    Out-of-app receipts lacking the main obfuscated account ID are deliberately
    rejected. They require a separate, trusted historical-account recovery flow.
    """
    if not isinstance(product_id, str) or not isinstance(product_type, str):
        raise PlayError("invalid_purchase")
    spec = PRODUCTS.get(product_id)
    if spec is None or product_type != spec.product_type:
        raise PlayError("invalid_purchase")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PlayError("invalid_purchase")
    now = now.astimezone(timezone.utc)
    payload = _object(payload)
    acknowledged = _acknowledged(payload)

    if product_type == "subs":
        item = _one_item(payload.get("lineItems"))
        offer = _object(item.get("offerDetails"))
        plan = _object(item.get("autoRenewingPlan"))
        if (
            item.get("productId") != product_id
            or offer.get("basePlanId") != spec.base_plan_id
            or offer.get("offerId") not in (None, "")
            or "prepaidPlan" in item
            or "installmentDetails" in plan
            or "deferredItemReplacement" in item
            or "deferredItemRemoval" in item
        ):
            raise PlayError("invalid_purchase")
        identifiers = _object(payload.get("externalAccountIdentifiers"))
        account_id = _account(identifiers.get("obfuscatedExternalAccountId"))
        state = payload.get("subscriptionState")
        if not isinstance(state, str):
            raise PlayError("invalid_purchase")
        pending = state in {
            "SUBSCRIPTION_STATE_PENDING", "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED"
        }
        entitlement_states = {
            "SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
            "SUBSCRIPTION_STATE_CANCELED",
        }
        if state not in entitlement_states | {
            "SUBSCRIPTION_STATE_PENDING", "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED",
            "SUBSCRIPTION_STATE_EXPIRED", "SUBSCRIPTION_STATE_ON_HOLD",
            "SUBSCRIPTION_STATE_PAUSED",
        }:
            raise PlayError("invalid_purchase")
        expires_at = _timestamp(item["expiryTime"]) if "expiryTime" in item else None
        if not pending and expires_at is None:
            raise PlayError("invalid_purchase")
        if pending and acknowledged:
            raise PlayError("invalid_purchase")
        # Display data never infers renewal from entitlement or expiry alone.
        # Missing flags remain unknown; strings/integers are not boolean truth.
        auto_renewing = plan.get("autoRenewEnabled")
        if type(auto_renewing) is not bool:
            auto_renewing = None
        next_billing_at = (
            expires_at if state == "SUBSCRIPTION_STATE_ACTIVE"
            and auto_renewing is True and expires_at is not None and expires_at > now
            else None
        )
        return PurchaseSnapshot(
            product_id, product_type, account_id,
            state in entitlement_states and expires_at is not None and expires_at > now,
            not pending, acknowledged,
            expires_at=expires_at,
            linked_token=None if pending else _optional_text(payload.get("linkedPurchaseToken")),
            order_id=_optional_text(item.get("latestSuccessfulOrderId")),
            subscription_state=state.removeprefix("SUBSCRIPTION_STATE_"),
            auto_renewing=auto_renewing,
            next_billing_at=next_billing_at,
        )

    item = _one_item(payload.get("productLineItem"))
    offer = _object(item.get("productOfferDetails"))
    if (
        item.get("productId") != product_id
        or offer.get("purchaseOptionId") != spec.purchase_option_id
        or offer.get("offerId") not in (None, "")
        or "rentOfferDetails" in offer
        or "preorderOfferDetails" in offer
        or type(offer.get("quantity")) is not int
        or offer.get("quantity") != 1
        or offer.get("consumptionState") != "CONSUMPTION_STATE_YET_TO_BE_CONSUMED"
    ):
        raise PlayError("invalid_purchase")
    account_id = _account(payload.get("obfuscatedExternalAccountId"))
    state = _object(payload.get("purchaseStateContext")).get("purchaseState")
    if not isinstance(state, str) or state not in {"PURCHASED", "PENDING", "CANCELLED"}:
        raise PlayError("invalid_purchase")
    if state == "PURCHASED":
        # Zero is a trusted full revocation; malformed or multi-quantity data is not.
        if type(offer.get("refundableQuantity")) is not int or offer.get("refundableQuantity") not in (0, 1):
            raise PlayError("invalid_purchase")
        _timestamp(payload.get("purchaseCompletionTime"))
    elif state == "PENDING" and acknowledged:
        raise PlayError("invalid_purchase")
    return PurchaseSnapshot(
        product_id, product_type, account_id,
        state == "PURCHASED" and offer.get("refundableQuantity") == 1,
        state != "PENDING", acknowledged,
        order_id=_optional_text(payload.get("orderId")),
        revoked=state == "CANCELLED" or state == "PURCHASED" and offer.get("refundableQuantity") == 0,
    )


_private_request = contextvars.ContextVar("google_play_private_request", default=False)


class _PrivateTransportLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _private_request.get():
            return False
        # httpx emits full request URLs, including purchase-token path segments.
        message = record.getMessage()
        return "androidpublisher.googleapis.com" not in message


_log_filter = _PrivateTransportLogs()


@dataclass
class _OAuthResponse:
    status: int
    data: bytes = field(repr=False)
    headers: Any = field(repr=False)


class _OAuthRequest:
    """The google-auth transport protocol with the same bounded HTTP client."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def __call__(
        self, url: str, method: str = "GET", body: Any = None,
        headers: Any = None, timeout: Any = None, **kwargs: Any,
    ) -> _OAuthResponse:
        if url not in {
            "https://oauth2.googleapis.com/token", "https://accounts.google.com/o/oauth2/token"
        }:
            raise PlayError("unavailable")
        response = self.client.request(
            method, url, content=body, headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False,
        )
        return _OAuthResponse(response.status_code, response.content, response.headers)


class PlayClient:
    def __init__(
        self, settings: BillingSettings, *, http_client: Optional[httpx.Client] = None,
        credentials: Any = None, auth_request: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.settings = settings
        self._http = http_client if http_client is not None else httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False,
        )
        self._owns_http = http_client is None
        self._credentials = credentials
        self._auth_request = auth_request if auth_request is not None else _OAuthRequest(self._http)
        self._credential_lock = threading.Lock()
        for name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy"):
            logger = logging.getLogger(name)
            if _log_filter not in logger.filters:
                logger.addFilter(_log_filter)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _headers(self, method: str, url: str) -> dict:
        with self._credential_lock:
            if self._credentials is None:
                try:
                    from google.oauth2 import service_account

                    self._credentials = service_account.Credentials.from_service_account_file(
                        self.settings.credentials_file, scopes=[ANDROIDPUBLISHER_SCOPE]
                    )
                except Exception:
                    raise PlayError("unavailable") from None
            headers = {"Accept": "application/json"}
            try:
                self._credentials.before_request(self._auth_request, method, url, headers)
            except Exception:
                raise PlayError("unavailable") from None
            return headers

    def _request(
        self, method: str, path: str, *, params: Optional[dict] = None,
        body: Optional[dict] = None, empty_response: bool = False,
    ) -> dict:
        marker = _private_request.set(True)
        try:
            url = f"{API_ROOT}/applications/{PACKAGE_NAME}/{path}"
            headers = self._headers(method, url)
            response = self._http.request(
                method, url, headers=headers, params=params, json=body,
                timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False,
            )
            if response.status_code == 404:
                raise PlayError("not_found")
            if response.status_code in {400, 410}:
                raise PlayError("invalid_purchase")
            if not 200 <= response.status_code < 300:
                raise PlayError("unavailable")
            if empty_response:
                return {}
            data = response.json()
            if not isinstance(data, dict):
                raise PlayError("unavailable")
            return data
        except PlayError:
            raise
        except Exception:
            raise PlayError("unavailable") from None
        finally:
            _private_request.reset(marker)

    @staticmethod
    def _token_path(token: str) -> str:
        if (
            not isinstance(token, str) or not token or len(token) > 16384
            or token in {".", ".."} or any(ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            raise PlayError("invalid_purchase")
        return quote(token, safe="")

    def get_subscription(self, token: str) -> dict:
        return self._request("GET", f"purchases/subscriptionsv2/tokens/{self._token_path(token)}")

    def get_product(self, token: str) -> dict:
        return self._request("GET", f"purchases/productsv2/tokens/{self._token_path(token)}")

    def acknowledge(self, product_id: str, product_type: str, token: str) -> None:
        spec = PRODUCTS.get(product_id)
        if spec is None or spec.product_type != product_type:
            raise PlayError("invalid_purchase")
        resource = "subscriptions" if product_type == "subs" else "products"
        self._request(
            "POST", f"purchases/{resource}/{product_id}/tokens/{self._token_path(token)}:acknowledge",
            body={}, empty_response=True,
        )

    def list_voided(self, start_ms: int, end_ms: int, page_token: Optional[str] = None) -> dict:
        if type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or end_ms < start_ms:
            raise PlayError("invalid_purchase")
        params = {
            "startTime": str(start_ms), "endTime": str(end_ms), "type": 1,
            "includeQuantityBasedPartialRefund": "true", "maxResults": 1000,
        }
        if page_token is not None:
            self._token_path(page_token)
            # Query parameters are escaped by httpx, not by path quoting.
            params["token"] = page_token
        return self._request("GET", "purchases/voidedpurchases", params=params)

    def catalog_ready(self) -> bool:
        """Read all three products; return false for any unknown catalog shape."""
        if not self.settings.configuration_complete:
            return False
        try:
            for product_id, spec in PRODUCTS.items():
                resource = "subscriptions" if spec.product_type == "subs" else "oneTimeProducts"
                data = self._request("GET", f"{resource}/{product_id}")
                if data.get("packageName") != PACKAGE_NAME or data.get("productId") != product_id:
                    return False
                if spec.product_type == "subs":
                    if not self._subscription_catalog(data, spec):
                        return False
                elif not self._product_catalog(data, spec):
                    return False
            return True
        except (PlayError, ValueError, TypeError, KeyError):
            return False

    @staticmethod
    def _price(value: Any, expected: int) -> bool:
        if not isinstance(value, dict) or value.get("currencyCode") != "KRW":
            return False
        units = value.get("units")
        nanos = value.get("nanos", 0)
        return (
            (type(units) is int and units == expected or type(units) is str and units == str(expected))
            and type(nanos) is int and nanos == 0
        )

    @classmethod
    def _subscription_catalog(cls, data: dict, spec: ProductSpec) -> bool:
        plans = data.get("basePlans")
        if not isinstance(plans, list) or len(plans) != 1 or not isinstance(plans[0], dict):
            return False
        plan = plans[0]
        auto = plan.get("autoRenewingBasePlanType")
        if (
            plan.get("basePlanId") != spec.base_plan_id or plan.get("state") != "ACTIVE"
            or not isinstance(auto, dict) or auto.get("billingPeriodDuration") != spec.billing_period
            or "prepaidBasePlanType" in plan or "installmentsBasePlanType" in plan
        ):
            return False
        regions = plan.get("regionalConfigs")
        if not isinstance(regions, list) or any(not isinstance(r, dict) for r in regions):
            return False
        korea = [r for r in regions if r.get("regionCode") == "KR"]
        return (
            len(korea) == 1 and korea[0].get("newSubscriberAvailability") is True
            and cls._price(korea[0].get("price"), spec.krw_price)
            and all(r.get("newSubscriberAvailability", False) is False for r in regions if r.get("regionCode") != "KR")
            and _object(plan.get("otherRegionsConfig", {})).get("newSubscriberAvailability", False) is False
        )

    @classmethod
    def _product_catalog(cls, data: dict, spec: ProductSpec) -> bool:
        options = data.get("purchaseOptions")
        if not isinstance(options, list) or len(options) != 1 or not isinstance(options[0], dict):
            return False
        option = options[0]
        buy = option.get("buyOption")
        if (
            option.get("purchaseOptionId") != spec.purchase_option_id or option.get("state") != "ACTIVE"
            or not isinstance(buy, dict) or buy.get("multiQuantityEnabled", False) is not False
            or "rentOption" in option
        ):
            return False
        regions = option.get("regionalPricingAndAvailabilityConfigs")
        if not isinstance(regions, list) or any(not isinstance(r, dict) for r in regions):
            return False
        korea = [r for r in regions if r.get("regionCode") == "KR"]
        return (
            len(korea) == 1 and korea[0].get("availability") == "AVAILABLE"
            and cls._price(korea[0].get("price"), spec.krw_price)
            and all(r.get("availability") != "AVAILABLE" for r in regions if r.get("regionCode") != "KR")
            and _object(option.get("newRegionsConfig", {})).get("availability") != "AVAILABLE"
        )
