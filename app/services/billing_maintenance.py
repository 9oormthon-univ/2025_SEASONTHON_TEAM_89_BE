"""Single-worker recovery; no background I/O until sales or pilot testing is enabled."""
from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool


LOG = logging.getLogger(__name__)
INTERVAL_SECONDS = 15 * 60


def maintenance_once() -> None:
    from app.services.play_billing_client import BillingSettings

    settings = BillingSettings.from_env()
    # Test the sales/pilot gate first: configuration_complete inspects secret-file metadata.
    if not settings.maintenance_enabled:
        return
    if not settings.configuration_complete:
        LOG.warning("billing_maintenance_not_configured")
        return

    from app.api.endpoints.billing import service_for
    from app.core.database import SessionLocal

    # A failed acknowledgement must not prevent a refund check, or reuse its transaction.
    for operation in ("retry_unacknowledged", "sync_voided"):
        try:
            with SessionLocal() as db:
                result = getattr(service_for(db, settings, require_enabled=False), operation)()
            if operation == "retry_unacknowledged":
                checked, failed = int(result["checked"]), int(result["failed"])
                log = LOG.warning if failed else LOG.info
                log("billing_acknowledgement_retry checked=%d failed=%d", checked, failed)
            else:
                LOG.info("billing_voided_sync_complete checked=%d", int(result))
        except Exception:
            # Never log receipt tokens, user IDs, request URLs or exception text.
            LOG.warning("billing_%s_failed", operation)


async def billing_maintenance_loop() -> None:
    """Keep serving requests during recovery and retry failed work next interval.

    Cancellation stops subsequent runs. An already running blocking operation owns
    and closes its DB session when its bounded Google calls return.
    """
    while True:
        try:
            await run_in_threadpool(maintenance_once)
        except Exception:
            LOG.warning("billing_maintenance_failed")
        await asyncio.sleep(INTERVAL_SECONDS)
