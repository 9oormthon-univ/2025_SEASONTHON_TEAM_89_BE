"""Minimize purchase data inside the caller's account-deletion transaction."""
from sqlalchemy import inspect

from app.models.play_billing import BillingAccount, BillingPurchase


# These fixed values cannot identify a former user or be claimed by a UUID actor.
DELETED_BILLING_OWNER = "__deleted_billing_owner__"
DELETED_BILLING_PRODUCT_TYPE = "deleted"


class BillingAccountDeletionError(RuntimeError):
    """A fixed message prevents database exceptions from exposing receipt data."""

    def __init__(self):
        super().__init__("billing_account_deletion_unavailable")


def delete_billing_account_data(db, user_id: str) -> int:
    """Retain token hashes only; never commit, query Google or load secret files.

    The caller must first lock the active User row. The reserved row keeps the
    unique token claim so a deleted purchase cannot move to a different account.
    """
    if not isinstance(user_id, str) or not user_id or user_id == DELETED_BILLING_OWNER:
        raise BillingAccountDeletionError()
    try:
        inspector = inspect(db.connection())
        present = [inspector.has_table(model.__tablename__)
                   for model in (BillingAccount, BillingPurchase)]
        if not any(present):
            # Older deployments without any billing tables have no data to scrub.
            return 0
        if not all(present):
            raise BillingAccountDeletionError()

        purchases = (db.query(BillingPurchase).filter_by(user_id=user_id)
                     .order_by(BillingPurchase.token_sha256)
                     .with_for_update().populate_existing().all())
        for purchase in purchases:
            purchase.user_id = DELETED_BILLING_OWNER
            purchase.token_encrypted = ""
            purchase.product_id = None
            purchase.product_type = DELETED_BILLING_PRODUCT_TYPE
            purchase.active = False
            purchase.acknowledged = False
            purchase.expires_at = None
            purchase.checked_at = None
            purchase.revoked_at = None
            purchase.superseded_by_hash = None
        accounts = (db.query(BillingAccount).filter_by(user_id=user_id)
                    .with_for_update().populate_existing().all())
        for account in accounts:
            db.delete(account)
        db.flush()
        return len(purchases)
    except BillingAccountDeletionError:
        raise
    except Exception:
        raise BillingAccountDeletionError() from None
