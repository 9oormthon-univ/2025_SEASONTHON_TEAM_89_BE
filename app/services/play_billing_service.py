"""Google-verified receipts only. No local premium switch or synthetic entitlement."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from app.models.play_billing import BillingAccount, BillingPurchase, BillingSyncState
from app.models.user import User
from app.services.billing_account_deletion import DELETED_BILLING_OWNER
from app.services.play_billing_client import PRODUCTS, PlayError, snapshot

UTC = timezone.utc
LEASE = timedelta(minutes=5)


class BillingError(Exception):
    def __init__(self, code: str = "billing_unavailable", status: int = 503):
        self.code, self.status = code, status
        super().__init__(code)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    return utc(value).isoformat().replace("+00:00", "Z") if value else None


class TokenCipher:
    def __init__(self, keys: bytes | list[bytes]):
        try:
            self.cipher = MultiFernet([Fernet(k) for k in (keys if isinstance(keys, list) else [keys])])
        except Exception:
            raise BillingError("billing_not_configured") from None

    @classmethod
    def from_file(cls, filename: str) -> "TokenCipher":
        try:
            keys = [line.strip() for line in Path(filename).read_bytes().splitlines() if line.strip()]
            if not keys:
                raise ValueError()
            return cls(keys)
        except Exception:
            raise BillingError("billing_not_configured") from None

    def encrypt(self, token: str) -> str:
        return self.cipher.encrypt(token.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        try:
            return self.cipher.decrypt(encrypted.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            raise BillingError() from None


class BillingService:
    def __init__(self, db, play, codec: TokenCipher, now=None):
        self.db, self.play, self.codec = db, play, codec
        self.now = now or (lambda: datetime.now(UTC))

    def _active_user(self, user_id: str):
        # Deletion and receipt writes always lock the user before the purchase.
        # Refresh the identity map after external Google calls or another transaction.
        user = (self.db.query(User).filter_by(user_id=user_id, is_active=True)
                .populate_existing().with_for_update().first())
        if user is None:
            raise BillingError("purchase_account_unavailable", 409)
        return user

    def register_account(self, user_id: str) -> None:
        self._active_user(user_id)
        expected = token_hash(user_id)
        account = self.db.get(BillingAccount, user_id)
        if account is None:
            self.db.add(BillingAccount(user_id=user_id, account_hash=expected))
            try:
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                account = self.db.get(BillingAccount, user_id)
                if account is None or not hmac.compare_digest(account.account_hash, expected):
                    raise BillingError("purchase_account_mismatch", 409) from None
        elif not hmac.compare_digest(account.account_hash, expected):
            raise BillingError("purchase_account_mismatch", 409)
        self.db.commit()

    def _owned(self, digest: str, user_id: str, *, lock=False):
        query = self.db.query(BillingPurchase).filter_by(token_sha256=digest)
        if lock:
            query = query.populate_existing().with_for_update()
        record = query.first()
        if record is not None and record.user_id != user_id:
            raise BillingError("purchase_account_mismatch", 409)
        return record

    def _fetch(self, product_id: str, product_type: str, token: str):
        payload = (self.play.get_subscription(token) if product_type == "subs"
                   else self.play.get_product(token))
        return snapshot(payload, product_id, product_type, utc(self.now()))

    def _fetch_for_user(self, user_id: str, product_id: str, product_type: str, token: str):
        # Serialize state reads as well as writes. Otherwise a delayed ACTIVE
        # Google response could overwrite a newer HOLD/EXPIRED notification.
        self._active_user(user_id)
        self._owned(token_hash(token), user_id, lock=True)
        return self._fetch(product_id, product_type, token)

    def _accept(self, user_id: str, token: str, purchase):
        if not hmac.compare_digest(purchase.account_id, token_hash(user_id)):
            raise BillingError("purchase_account_mismatch", 403)
        if not purchase.completed:
            # In particular, a pending canceled replacement cannot revoke its predecessor.
            raise BillingError("purchase_pending", 409)
        digest, now = token_hash(token), utc(self.now())
        try:
            self._active_user(user_id)
            record = self._owned(digest, user_id, lock=True)
            if record is None:
                record = BillingPurchase(token_sha256=digest, user_id=user_id,
                    token_encrypted=self.codec.encrypt(token), product_id=purchase.product_id,
                    product_type=purchase.product_type)
                self.db.add(record)
            elif record.product_id not in (None, purchase.product_id) or record.product_type != purchase.product_type:
                raise BillingError("purchase_product_mismatch", 409)
            # Ownership and replacement reservation are committed atomically before acknowledgement.
            if purchase.linked_token:
                linked_digest = token_hash(purchase.linked_token)
                if linked_digest == digest:
                    raise BillingError("invalid_purchase", 409)
                predecessor = self._owned(linked_digest, user_id, lock=True)
                if predecessor is None:
                    predecessor = BillingPurchase(token_sha256=linked_digest, user_id=user_id,
                        token_encrypted=self.codec.encrypt(purchase.linked_token), product_type="subs")
                    self.db.add(predecessor)
                predecessor.active = False
                predecessor.superseded_by_hash = digest
                predecessor.checked_at = now
            record.product_id = purchase.product_id
            if purchase.product_type == "inapp" and purchase.revoked:
                record.revoked_at = now
            record.active = purchase.active and not record.revoked_at and not record.superseded_by_hash
            record.acknowledged = purchase.acknowledged
            record.expires_at, record.checked_at = purchase.expires_at, now
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            # A conflicting unique token claim is never allowed to transfer ownership.
            existing = self.db.get(BillingPurchase, digest)
            if existing is not None and existing.user_id != user_id:
                raise BillingError("purchase_account_mismatch", 409) from None
            raise BillingError("purchase_retry", 503) from None
        except Exception:
            self.db.rollback()
            raise
        if record.active and not purchase.acknowledged:
            try:
                self.play.acknowledge(purchase.product_id, purchase.product_type, token)
            except PlayError:
                # Never deliver a paid lease until acknowledgement succeeds. A retry re-queries Play.
                self._active_user(user_id)
                record = self._owned(digest, user_id, lock=True)
                record.active = False
                record.checked_at = None
                self.db.commit()
                raise BillingError("purchase_acknowledgement_pending") from None
            self._active_user(user_id)
            record = self._owned(digest, user_id, lock=True)
            record.acknowledged = True
            self.db.commit()
        return record

    def _mark_inactive(self, user_id, digest, *, revoked=False):
        try:
            self._active_user(user_id)
        except BillingError:
            self.db.rollback()
            return (self.db.query(BillingPurchase).filter_by(token_sha256=digest)
                    .populate_existing().first())
        record = self._owned(digest, user_id, lock=True)
        if record is not None:
            record.active, record.checked_at = False, utc(self.now())
            if revoked:
                record.revoked_at = utc(self.now())
        self.db.commit()
        return record

    def verify(self, user_id: str, product_id: str, product_type: str, token: str) -> dict:
        spec = PRODUCTS.get(product_id)
        if spec is None or spec.product_type != product_type:
            raise BillingError("invalid_product", 400)
        self.register_account(user_id)
        self._owned(token_hash(token), user_id)
        try:
            purchase = self._fetch_for_user(user_id, product_id, product_type, token)
            record = self._accept(user_id, token, purchase)
        except PlayError as error:
            raise BillingError("invalid_purchase" if error.code in ("invalid_purchase", "not_found")
                               else "billing_unavailable", 400 if error.code == "invalid_purchase" else 503) from None
        return {"verified": True, "product_id": product_id,
                "purchase_token_sha256": token_hash(token), "acknowledged": bool(record.acknowledged),
                "entitlement": self.entitlement(user_id)}

    def _reconcile(self, record):
        if record.user_id == DELETED_BILLING_OWNER or record.revoked_at or record.superseded_by_hash:
            return record
        owner, digest = record.user_id, record.token_sha256
        token = self.codec.decrypt(record.token_encrypted)
        try:
            return self._accept(owner, token,
                                self._fetch_for_user(owner, record.product_id, record.product_type, token))
        except PlayError as error:
            if error.code != "not_found":
                raise BillingError() from None
            return self._mark_inactive(owner, digest)
        except BillingError as error:
            if error.code != "purchase_pending":
                raise
            return self._mark_inactive(owner, digest)

    def entitlement(self, user_id: str, force=False) -> dict:
        self.register_account(user_id)
        now = utc(self.now())
        records = self.db.query(BillingPurchase).filter_by(user_id=user_id).all()
        candidates = []
        for record in records:
            if record.revoked_at or record.superseded_by_hash or record.product_id not in PRODUCTS:
                continue
            # Play no longer accepts subscription tokens 60 days after expiration.
            if record.expires_at and utc(record.expires_at) + timedelta(days=60) <= now:
                continue
            if (force or not record.checked_at or utc(record.checked_at) + LEASE <= now or
                (record.active and record.expires_at and utc(record.expires_at) <= now)):
                record = self._reconcile(record)
            if (record.active and record.acknowledged and record.checked_at and
                utc(record.checked_at) + LEASE > now and
                (record.product_type == "inapp" or (record.expires_at and utc(record.expires_at) > now))):
                candidates.append(record)
        if not candidates:
            self._active_user(user_id)
            self.db.commit()
            return {"user_id": user_id, "verified": True, "ad_free": False, "product_id": None,
                    "expires_at": None, "cache_until": iso(now + LEASE)}
        # Lifetime wins if the Play account has overlapping purchases; don't charge for another plan.
        record = max(candidates, key=lambda p: (p.product_type == "inapp",
                     utc(p.expires_at) if p.expires_at else datetime.max.replace(tzinfo=UTC)))
        self._active_user(user_id)
        record = self._owned(record.token_sha256, user_id, lock=True)
        if not (record.active and record.acknowledged and record.checked_at and
                utc(record.checked_at) + LEASE > now and
                (record.product_type == "inapp" or (record.expires_at and utc(record.expires_at) > now))):
            self.db.commit()
            return {"user_id": user_id, "verified": True, "ad_free": False, "product_id": None,
                    "expires_at": None, "cache_until": iso(now + LEASE)}
        until = min(now + LEASE, utc(record.checked_at) + LEASE,
                    utc(record.expires_at) if record.expires_at else now + LEASE)
        result = {"user_id": user_id, "verified": True, "ad_free": True,
                "product_id": record.product_id, "expires_at": iso(record.expires_at), "cache_until": iso(until)}
        self.db.commit()
        return result

    def reconcile_notification(self, token: str, product_type: str) -> None:
        record = self.db.get(BillingPurchase, token_hash(token))
        if record is not None:
            user = self.db.query(User).filter_by(user_id=record.user_id, is_active=True).first()
            if user is not None:
                self._reconcile(record)
            return
        payload = self.play.get_subscription(token) if product_type == "subs" else self.play.get_product(token)
        items = payload.get("lineItems" if product_type == "subs" else "productLineItem", [])
        products = {item.get("productId") for item in items if isinstance(item, dict)}
        if len(products) != 1:
            raise BillingError("invalid_purchase", 400)
        product_id = next(iter(products))
        if product_id not in PRODUCTS:
            return  # Other app products are not ad-free receipts.
        purchase = snapshot(payload, product_id, product_type, utc(self.now()))
        account = self.db.query(BillingAccount).filter_by(account_hash=purchase.account_id).first()
        if account is None:
            return  # No authenticated account has registered this mapping; don't invent a user.
        user = self.db.query(User).filter_by(user_id=account.user_id, is_active=True).first()
        if user is None:
            return
        try:
            # The initial lookup only discovers the account. Read current state
            # again under its lock before writing or acknowledging a new receipt.
            self._accept(account.user_id, token,
                         self._fetch_for_user(account.user_id, product_id, product_type, token))
        except BillingError as error:
            if error.code != "purchase_pending":
                raise

    def handle_voided(self, event: dict, *, from_google_api=False) -> None:
        token = event.get("purchaseToken")
        if not isinstance(token, str) or not token:
            raise BillingError("invalid_notification", 400)
        record = self.db.get(BillingPurchase, token_hash(token))
        if record is None or record.user_id == DELETED_BILLING_OWNER:
            return
        if record.product_type == "subs":
            # A refund of an old renewal shares its token with later renewals. Current Play state wins.
            self._reconcile(record)
        else:
            # Always re-query Play. Pub/Sub content alone cannot establish a refund.
            record = self._reconcile(record)
            if record.active and not from_google_api:
                # Google state may lag the event. Return non-2xx so Pub/Sub retries it.
                raise BillingError("purchase_revocation_pending")
            # A completed Google Voided Purchases list response is also authoritative.
            # Lifetime quantity is strictly one; don't apply multi-quantity partial-refund logic.
            quantity = event.get("voidedQuantity", 1)
            if quantity != 1:
                raise BillingError("invalid_notification", 400)
            if record is not None and record.user_id != DELETED_BILLING_OWNER:
                self._mark_inactive(record.user_id, record.token_sha256, revoked=True)

    def retry_unacknowledged(self, limit=100) -> dict:
        # Bound each maintenance cycle and isolate failures so one receipt cannot
        # starve acknowledgement of unrelated purchases.
        records = (self.db.query(BillingPurchase).join(User, User.user_id == BillingPurchase.user_id)
                   .filter(User.is_active.is_(True), BillingPurchase.acknowledged.is_(False),
                           BillingPurchase.revoked_at.is_(None), BillingPurchase.superseded_by_hash.is_(None),
                           BillingPurchase.token_encrypted != "",
                           or_(and_(BillingPurchase.product_type == "subs",
                                    BillingPurchase.product_id.in_(["ad_free_monthly", "ad_free_yearly"]),
                                    BillingPurchase.expires_at > utc(self.now())),
                               and_(BillingPurchase.product_type == "inapp",
                                    BillingPurchase.product_id == "ad_free_lifetime",
                                    BillingPurchase.expires_at.is_(None))))
                   .order_by(BillingPurchase.checked_at, BillingPurchase.token_sha256).limit(limit).all())
        digests = [record.token_sha256 for record in records]
        self.db.rollback()
        checked, failed = 0, 0
        for digest in digests:
            owner = None
            try:
                record = self.db.get(BillingPurchase, digest)
                if record is not None:
                    owner = record.user_id
                    self._reconcile(record)
                    checked += 1
            except Exception:
                self.db.rollback()
                failed += 1
                # Move a failed receipt to the back of the bounded retry queue.
                # It still cannot grant access because active is false.
                if owner and owner != DELETED_BILLING_OWNER:
                    try:
                        self._mark_inactive(owner, digest)
                    except Exception:
                        self.db.rollback()
        return {"checked": checked, "failed": failed}

    def sync_voided(self) -> int:
        now = utc(self.now())
        state = self.db.get(BillingSyncState, "voided")
        start = max(now - timedelta(days=30),
                    utc(state.checked_until) - timedelta(hours=1) if state else now - timedelta(days=30))
        page, count, seen = None, 0, set()
        while True:
            data = self.play.list_voided(int(start.timestamp() * 1000), int(now.timestamp() * 1000), page)
            for event in data.get("voidedPurchases", []):
                self.handle_voided(event, from_google_api=True)
                count += 1
            next_page = data.get("tokenPagination", {}).get("nextPageToken")
            if not next_page:
                break
            if not isinstance(next_page, str) or next_page in seen or len(seen) >= 1000:
                raise BillingError()
            seen.add(next_page)
            page = next_page
        if state is None:
            state = BillingSyncState(name="voided", checked_until=now)
            self.db.add(state)
        else:
            state.checked_until = now
        self.db.commit()
        return count
