"""Authenticated Android contract plus authenticated Google Pub/Sub delivery."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import require_current_user
from app.core.database import get_db
from app.models.play_billing import BILLING_TABLES, BillingNotification
from app.services.play_billing_client import BillingSettings, PACKAGE_NAME, PRODUCTS, PlayClient, PlayError
from app.services.play_billing_service import BillingError, BillingService, TokenCipher

router = APIRouter()
_runtime_lock = threading.RLock()
_runtime_cache = None
_catalog_cache = None


def get_settings() -> BillingSettings:
    return BillingSettings.from_env()


def runtime(settings: BillingSettings):
    """Reuse OAuth credentials; invalidate on configuration/secret-file rotation."""
    global _runtime_cache
    if not settings.configuration_complete:
        raise BillingError("billing_not_configured")
    try:
        key = (settings, Path(settings.credentials_file).stat().st_mtime_ns,
               Path(settings.token_key_file).stat().st_mtime_ns)
        with _runtime_lock:
            if _runtime_cache is None or _runtime_cache[0] != key:
                play = PlayClient(settings)
                codec = TokenCipher.from_file(settings.token_key_file)
                _runtime_cache = (key, play, codec)
            return _runtime_cache[1:]
    except (PlayError, OSError):
        raise BillingError("billing_not_configured") from None


def schema_ready(db) -> bool:
    inspector = inspect(db.get_bind())
    return all(inspector.has_table(table.name) for table in BILLING_TABLES)


def service_for(db, settings: BillingSettings, *, require_enabled=True) -> BillingService:
    if require_enabled and not settings.enabled:
        raise BillingError("billing_not_ready")
    if not schema_ready(db):
        raise BillingError("billing_not_configured")
    play, codec = runtime(settings)
    return BillingService(db, play, codec)


def raise_safe(error: Exception):
    if isinstance(error, BillingError):
        raise HTTPException(error.status, error.code) from None
    raise HTTPException(503, "billing_unavailable") from None


def catalog_ready(play) -> bool:
    global _catalog_cache
    with _runtime_lock:
        now = time.monotonic()
        if _catalog_cache is None or _catalog_cache[0] is not play or _catalog_cache[1] <= now:
            _catalog_cache = (play, now + 60, play.catalog_ready())
        return _catalog_cache[2]


@router.get("/config", summary="광고 제거 판매 준비 상태")
def config(db=Depends(get_db), current_user=Depends(require_current_user), settings=Depends(get_settings)):
    ready = False
    if settings.enabled:
        try:
            service = service_for(db, settings)
            ready = catalog_ready(service.play)
            if ready:
                service.register_account(str(current_user.user_id))
        except Exception:
            db.rollback()
            ready = False
    return {"api_version": 1, "google_play_ready": ready, "product_ids": list(PRODUCTS)}


@router.get("/entitlement", summary="검증된 광고 제거 이용권")
def entitlement(db=Depends(get_db), current_user=Depends(require_current_user), settings=Depends(get_settings)):
    try:
        return service_for(db, settings).entitlement(str(current_user.user_id))
    except Exception as error:
        db.rollback()
        raise_safe(error)


async def bounded_json(request: Request, limit: int) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "billing_request_too_large")
        body.extend(chunk)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise HTTPException(400, "invalid_billing_request") from None


def verify_fields(data: dict):
    expected = {"package_name", "product_id", "product_type", "purchase_token"}
    if set(data) != expected or any(not isinstance(data[k], str) for k in expected):
        raise HTTPException(400, "invalid_billing_request")
    product = PRODUCTS.get(data["product_id"])
    token = data["purchase_token"]
    if (data["package_name"] != PACKAGE_NAME or product is None or
        data["product_type"] != product.product_type or
        not 8 <= len(token) <= 4096 or not re.fullmatch(r"[\x21-\x7e]+", token)):
        raise HTTPException(400, "invalid_billing_request")


@router.post("/google-play/verify", summary="Google Play 구매 검증과 승인", openapi_extra={
    "requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "additionalProperties": False,
        "required": ["package_name", "product_id", "product_type", "purchase_token"],
        "properties": {"package_name": {"type": "string"}, "product_id": {"type": "string"},
                       "product_type": {"type": "string", "enum": ["subs", "inapp"]},
                       "purchase_token": {"type": "string", "writeOnly": True, "maxLength": 4096}},
    }}}},
})
async def verify(request: Request, db=Depends(get_db), current_user=Depends(require_current_user),
                 settings=Depends(get_settings)):
    # Parse manually so FastAPI validation errors never echo the confidential receipt token.
    data = await bounded_json(request, 8192)
    verify_fields(data)
    user_id = str(current_user.user_id)
    try:
        return await run_in_threadpool(lambda: service_for(db, settings).verify(
            user_id, data["product_id"], data["product_type"], data["purchase_token"]))
    except Exception as error:
        await run_in_threadpool(db.rollback)
        raise_safe(error)


def authenticate_push(request: Request, settings: BillingSettings) -> None:
    if not settings.configuration_complete:
        raise HTTPException(503, "billing_not_configured")
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or not 16 <= len(authorization) <= 8192:
        raise HTTPException(401, "invalid_push_authentication")
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2.id_token import verify_oauth2_token

        class TimedRequest(GoogleRequest):
            def __call__(self, *args, **kwargs):
                kwargs["timeout"] = 8
                return super().__call__(*args, **kwargs)

        claims = verify_oauth2_token(authorization[7:], TimedRequest(), audience=settings.rtdn_audience)
        if claims.get("email") != settings.rtdn_email or claims.get("email_verified") is not True:
            raise ValueError()
    except Exception:
        raise HTTPException(401, "invalid_push_authentication") from None


def decode_notification(envelope: dict, settings: BillingSettings):
    try:
        if envelope.get("subscription") != settings.rtdn_subscription:
            raise ValueError()
        message = envelope["message"]
        message_id, encoded = message["messageId"], message["data"]
        if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id):
            raise ValueError()
        if not isinstance(encoded, str) or len(encoded) > 32768:
            raise ValueError()
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) > 16384:
            raise ValueError()
        data = json.loads(decoded)
        if not isinstance(data, dict) or data.get("packageName") != PACKAGE_NAME:
            raise ValueError()
        kinds = ("subscriptionNotification", "oneTimeProductNotification",
                 "voidedPurchaseNotification", "testNotification")
        present = [kind for kind in kinds if kind in data]
        if len(present) != 1 or not isinstance(data[present[0]], dict):
            raise ValueError()
        event = data[present[0]]
        if present[0] != "testNotification":
            token = event.get("purchaseToken")
            if not isinstance(token, str) or not 8 <= len(token) <= 4096 or not re.fullmatch(r"[\x21-\x7e]+", token):
                raise ValueError()
        return message_id, present[0], event
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise HTTPException(400, "invalid_notification") from None


@router.post("/google-play/rtdn", status_code=204, summary="인증된 Google Play 변경 알림")
async def rtdn(request: Request, db=Depends(get_db), settings=Depends(get_settings)):
    await run_in_threadpool(authenticate_push, request, settings)
    envelope = await bounded_json(request, 65536)
    message_id, kind, event = decode_notification(envelope, settings)
    return await run_in_threadpool(process_notification, db, settings, message_id, kind, event)


def process_notification(db, settings, message_id, kind, event):
    try:
        if db.get(BillingNotification, message_id) is not None:
            return Response(status_code=204)
        # RTDN can validate delivery before new checkout is enabled. Every grant still queries Play.
        service = service_for(db, settings, require_enabled=False)
        if kind == "voidedPurchaseNotification":
            service.handle_voided(event)
        elif kind != "testNotification":
            service.reconcile_notification(event["purchaseToken"],
                "subs" if kind == "subscriptionNotification" else "inapp")
        db.add(BillingNotification(message_id=message_id, received_at=datetime.now(timezone.utc)))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if db.get(BillingNotification, message_id) is None:
                raise
        return Response(status_code=204)
    except Exception as error:
        db.rollback()
        raise_safe(error)
