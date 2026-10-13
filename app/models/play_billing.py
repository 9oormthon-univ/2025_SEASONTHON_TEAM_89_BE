"""Receipt ownership persists even after account deletion; tokens are encrypted."""
from sqlalchemy import Boolean, Column, DateTime, String, Text

from app.core.database import Base


class BillingAccount(Base):
    __tablename__ = "billing_accounts"
    user_id = Column(String(36), primary_key=True)
    account_hash = Column(String(64), nullable=False, unique=True)


class BillingPurchase(Base):
    __tablename__ = "billing_purchases"
    token_sha256 = Column(String(64), primary_key=True)
    user_id = Column(String(36), nullable=False, index=True)
    token_encrypted = Column(Text, nullable=False)
    # A trusted replacement can reserve an unseen predecessor as a tombstone.
    product_id = Column(String(40), nullable=True)
    product_type = Column(String(8), nullable=False)
    active = Column(Boolean, nullable=False, default=False)
    acknowledged = Column(Boolean, nullable=False, default=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    checked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    superseded_by_hash = Column(String(64), nullable=True)


class BillingNotification(Base):
    __tablename__ = "billing_notifications"
    message_id = Column(String(128), primary_key=True)
    received_at = Column(DateTime(timezone=True), nullable=False)


class BillingSyncState(Base):
    __tablename__ = "billing_sync_state"
    name = Column(String(40), primary_key=True)
    checked_until = Column(DateTime(timezone=True), nullable=False)


BILLING_TABLES = (BillingAccount.__table__, BillingPurchase.__table__,
                  BillingNotification.__table__, BillingSyncState.__table__)
