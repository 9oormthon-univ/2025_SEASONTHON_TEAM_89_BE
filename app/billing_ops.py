"""Explicit operator commands. No migration or enabling happens on API startup."""
import argparse
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


_SETTINGS = (
    ("GOOGLE_PLAY_SERVICE_ACCOUNT_FILE", "credentials_file"),
    ("GOOGLE_PLAY_TOKEN_KEY_FILE", "token_key_file"),
    ("GOOGLE_PLAY_RTDN_AUDIENCE", "rtdn_audience"),
    ("GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL", "rtdn_email"),
    ("GOOGLE_PLAY_RTDN_SUBSCRIPTION", "rtdn_subscription"),
)


def configuration_issues(settings):
    """Inspect names, format and file metadata only; never return values or paths."""
    missing, invalid = [], []
    if not settings.test_users_valid:
        invalid.append({"name": "GOOGLE_PLAY_BILLING_TEST_USER_IDS", "reason": "invalid_uuid_list"})
    for name, attribute in _SETTINGS:
        value = getattr(settings, attribute, "")
        if not value:
            missing.append(name)
            continue
        reason = None
        if not isinstance(value, str):
            reason = "invalid_format"
        elif attribute in ("credentials_file", "token_key_file"):
            try:
                path = Path(value)
                if not path.is_absolute():
                    reason = "absolute_path_required"
                elif not path.exists():
                    reason = "file_missing"
                elif not path.is_file():
                    reason = "regular_file_required"
                elif not os.access(path, os.R_OK):
                    reason = "file_unreadable"
            except (OSError, ValueError):
                reason = "file_metadata_unavailable"
        elif attribute == "rtdn_email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.gserviceaccount\.com", value):
            reason = "service_account_email_required"
        elif attribute == "rtdn_subscription" and not re.fullmatch(r"projects/[^/\s]+/subscriptions/[^/\s]+", value):
            reason = "subscription_resource_required"
        if reason:
            invalid.append({"name": name, "reason": reason})
    return missing, invalid


def _failure(stage, reason, **details):
    # All callers supply fixed strings or allowlisted setting/table names, never exception text.
    return {"ok": False, "error": "billing_operator_check_failed",
            "stage": stage, "reason": reason, **details}


@contextmanager
def _quiet_logging():
    # Even SQL_ECHO/pre-existing handlers must not print private SQL/connection details.
    previous = logging.root.manager.disable
    logging.disable(max(previous, logging.CRITICAL))
    try:
        yield
    finally:
        logging.disable(previous)


def check_setup(settings):
    enabled = bool(settings.enabled)
    missing, invalid = configuration_issues(settings)
    if missing or invalid:
        return _failure("configuration", "missing_environment" if missing else "invalid_configuration",
                        missing_env=missing, invalid_fields=invalid, checkout_enabled=enabled)
    with _quiet_logging():
        return _check_runtime(settings, enabled)


def _check_runtime(settings, enabled):
    stage = "database"
    try:
        # Missing billing settings never import/open the production database or contact Google.
        from sqlalchemy import inspect
        from app.core.database import SessionLocal
        from app.models.play_billing import BILLING_TABLES
        from app.services.play_billing_client import PlayClient
        from app.services.play_billing_service import TokenCipher

        with SessionLocal() as db:
            db.connection()
            stage = "schema"
            inspector = inspect(db.get_bind())
            missing_tables = [table.name for table in BILLING_TABLES if not inspector.has_table(table.name)]
            if missing_tables:
                return _failure(stage, "billing_tables_missing", missing_tables=missing_tables,
                                checkout_enabled=enabled)
            stage = "token_key"
            TokenCipher.from_file(settings.token_key_file)
            stage = "catalog"
            play = PlayClient(settings)
            try:
                ready = play.catalog_ready()
            finally:
                play.close()
            if not ready:
                return _failure(stage, "google_catalog_unavailable_or_mismatch",
                                configuration_validated=False, checkout_enabled=enabled,
                                checks=["android_publisher_api_and_service_account_permissions",
                                        "three_active_products_and_korean_prices",
                                        "monthly_yearly_periods_and_lifetime_buy_option"])
        return {"ok": True, "configuration_validated": True, "checkout_enabled": enabled}
    except Exception:
        reasons = {"database": "database_unavailable", "schema": "billing_schema_check_failed",
                   "token_key": "token_encryption_key_invalid", "catalog": "google_catalog_check_failed"}
        return _failure(stage, reasons[stage], checkout_enabled=enabled)


def status_snapshot(settings, now=None):
    """Read operational counts only; do not inspect keys or contact Google."""
    with _quiet_logging():
        return _status_runtime(settings, now or datetime.now(timezone.utc))


def _status_runtime(settings, now):
    fields = {"checkout_enabled": bool(settings.enabled),
              "test_account_count": len(settings.test_user_ids)}
    stage = "database"
    try:
        from sqlalchemy import and_, inspect, or_
        from app.core.database import SessionLocal
        from app.models.play_billing import BILLING_TABLES, BillingPurchase, BillingSyncState
        from app.models.user import User
        from app.services.play_billing_client import PRODUCTS

        with SessionLocal() as db:
            db.connection()
            stage = "schema"
            inspector = inspect(db.get_bind())
            missing = [table.name for table in BILLING_TABLES if not inspector.has_table(table.name)]
            if missing:
                return _failure(stage, "billing_tables_missing", missing_tables=missing,
                                schema_ready=False, **fields)
            stage = "status"
            state = db.get(BillingSyncState, "voided")
            stamp = state.checked_until if state else None
            if stamp is not None:
                stamp = (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None
                         else stamp.astimezone(timezone.utc)).isoformat().replace("+00:00", "Z")
            subs = [product for product, spec in PRODUCTS.items() if spec.product_type == "subs"]
            products = [product for product, spec in PRODUCTS.items() if spec.product_type == "inapp"]
            # checked_at=None is the marker left after an acknowledgement failure.
            # Expired subscriptions, deleted owners and replacement tombstones are not pending work.
            pending = db.query(BillingPurchase).join(User, User.user_id == BillingPurchase.user_id).filter(
                User.is_active.is_(True), BillingPurchase.acknowledged.is_(False),
                BillingPurchase.revoked_at.is_(None), BillingPurchase.superseded_by_hash.is_(None),
                BillingPurchase.token_encrypted != "",
                or_(and_(BillingPurchase.product_type == "subs", BillingPurchase.product_id.in_(subs),
                         BillingPurchase.expires_at > now),
                    and_(BillingPurchase.product_type == "inapp", BillingPurchase.product_id.in_(products),
                         BillingPurchase.expires_at.is_(None))),
            ).count()
        return {"ok": True, "schema_ready": True, "voided_last_success": stamp,
                "unacknowledged_purchases": pending, **fields}
    except Exception:
        reasons = {"database": "database_unavailable", "schema": "billing_schema_check_failed",
                   "status": "billing_status_check_failed"}
        return _failure(stage, reasons[stage], **fields)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("migrate", "check", "status", "sync-voided"))
    args = parser.parse_args()
    stage = "initialization"
    try:
        if args.command in ("check", "status"):
            from app.services.play_billing_client import BillingSettings
            settings = BillingSettings.from_env()
            result = check_setup(settings) if args.command == "check" else status_snapshot(settings)
            print(json.dumps(result))
            if not result["ok"]:
                raise SystemExit(1)
            return
        from app.core.database import engine, SessionLocal
        from app.models.play_billing import BILLING_TABLES
        from app.api.endpoints.billing import service_for
        from app.services.play_billing_client import BillingSettings

        if args.command == "migrate":
            stage = "migration"
            # Only new billing tables; don't change any existing user/family/ML table.
            for table in BILLING_TABLES:
                table.create(bind=engine, checkfirst=True)
            print(json.dumps({"billing_tables_created": True}))
            return
        with SessionLocal() as db:
            settings = BillingSettings.from_env()
            stage = "voided_sync"
            count = service_for(db, settings, require_enabled=False).sync_voided()
            print(json.dumps({"voided_orders_checked": count}))
    except SystemExit:
        raise
    except Exception:
        # SQL/Google exceptions can contain URLs or parameters; never print their details.
        print(json.dumps(_failure(stage, "operation_failed")))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
