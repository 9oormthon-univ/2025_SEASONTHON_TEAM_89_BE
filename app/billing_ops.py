"""Explicit operator commands. No migration or enabling happens on API startup."""
import argparse
import json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("migrate", "check", "sync-voided"))
    args = parser.parse_args()
    try:
        from app.core.database import engine, SessionLocal
        from app.models.play_billing import BILLING_TABLES
        from app.api.endpoints.billing import schema_ready, service_for
        from app.services.play_billing_client import BillingSettings

        if args.command == "migrate":
            # Only new billing tables; don't change any existing user/family/ML table.
            for table in BILLING_TABLES:
                table.create(bind=engine, checkfirst=True)
            print(json.dumps({"billing_tables_created": True}))
            return
        with SessionLocal() as db:
            settings = BillingSettings.from_env()
            if args.command == "check":
                service = service_for(db, settings, require_enabled=False)
                ready = schema_ready(db) and service.play.catalog_ready()
                print(json.dumps({"configuration_validated": ready, "checkout_enabled": settings.enabled}))
                if not ready:
                    raise SystemExit(1)
            else:
                count = service_for(db, settings, require_enabled=False).sync_voided()
                print(json.dumps({"voided_orders_checked": count}))
    except SystemExit:
        raise
    except Exception:
        # SQL/Google exceptions can contain URLs or parameters; never print their details.
        print(json.dumps({"ok": False, "error": "billing_operator_check_failed"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
