"""Account-deletion privacy regressions using the existing offline billing API."""
from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from test_play_billing import (
    api, isolated_modules, USER_A, USER_B, NOW, TOKEN, MONTHLY,
    digest, subscription, verify_http,
)


def deletion_module():
    return importlib.import_module("app.services.billing_account_deletion")


def seed_receipts(api):
    models = api.m.models
    predecessor = "synthetic-predecessor-token-for-deletion"
    other = "synthetic-other-user-token-for-deletion"
    with api.factory() as db:
        db.add_all([
            models.BillingAccount(user_id=USER_A, account_hash=digest(USER_A)),
            models.BillingAccount(user_id=USER_B, account_hash=digest(USER_B)),
            models.BillingPurchase(
                token_sha256=digest(TOKEN), user_id=USER_A,
                token_encrypted=api.codec.encrypt(TOKEN), product_id=MONTHLY,
                product_type="subs", active=True, acknowledged=True,
                expires_at=NOW + timedelta(days=30), checked_at=NOW,
                revoked_at=NOW, superseded_by_hash=digest(predecessor)),
            models.BillingPurchase(
                token_sha256=digest(predecessor), user_id=USER_A,
                token_encrypted=api.codec.encrypt(predecessor), product_id=None,
                product_type="subs", active=False, acknowledged=False,
                checked_at=NOW, superseded_by_hash=digest(TOKEN)),
            models.BillingPurchase(
                token_sha256=digest(other), user_id=USER_B,
                token_encrypted=api.codec.encrypt(other), product_id=MONTHLY,
                product_type="subs", active=True, acknowledged=True,
                expires_at=NOW + timedelta(days=30), checked_at=NOW),
        ])
        db.commit()
    return digest(predecessor), digest(other)


def assert_minimal_tombstone(record):
    deletion = deletion_module()
    assert record.user_id == deletion.DELETED_BILLING_OWNER
    assert record.product_type == deletion.DELETED_BILLING_PRODUCT_TYPE
    assert record.token_encrypted == "" and record.product_id is None
    assert not record.active and not record.acknowledged
    assert all(getattr(record, field) is None for field in (
        "expires_at", "checked_at", "revoked_at", "superseded_by_hash"))


def test_scrub_removes_receipt_details_and_predecessor_but_preserves_other_user(api):
    predecessor, other = seed_receipts(api)
    models, deletion = api.m.models, deletion_module()
    with api.factory() as db:
        assert deletion.delete_billing_account_data(db, USER_A) == 2
        db.commit()
    with api.factory() as db:
        for token_digest in (digest(TOKEN), predecessor):
            assert_minimal_tombstone(db.get(models.BillingPurchase, token_digest))
        assert db.get(models.BillingAccount, USER_A) is None
        assert db.get(models.BillingAccount, USER_B).account_hash == digest(USER_B)
        retained = db.get(models.BillingPurchase, other)
        assert retained.user_id == USER_B and retained.active and retained.acknowledged
        assert retained.token_encrypted and retained.product_id == MONTHLY
    assert api.play.calls == [] and api.play.ack_calls == []


def test_helper_flushes_without_committing_and_rollback_restores_all_data(api, monkeypatch):
    seed_receipts(api)
    models, deletion = api.m.models, deletion_module()
    with api.factory() as db:
        def forbidden_commit():
            raise AssertionError("Deletion helper must leave commit to its caller")
        monkeypatch.setattr(db, "commit", forbidden_commit)
        assert deletion.delete_billing_account_data(db, USER_A) == 2
        assert_minimal_tombstone(db.get(models.BillingPurchase, digest(TOKEN)))
        db.rollback()
    with api.factory() as db:
        original = db.get(models.BillingPurchase, digest(TOKEN))
        assert original.user_id == USER_A and original.token_encrypted
        assert original.product_id == MONTHLY and original.checked_at is not None
        assert db.get(models.BillingAccount, USER_A) is not None


def test_helper_deletes_account_mapping_even_without_purchase(api):
    models, deletion = api.m.models, deletion_module()
    with api.factory() as db:
        db.add(models.BillingAccount(user_id=USER_A, account_hash=digest(USER_A)))
        db.commit()
        assert deletion.delete_billing_account_data(db, USER_A) == 0
        db.commit()
        assert db.get(models.BillingAccount, USER_A) is None


def test_helper_is_idempotent_and_keeps_global_token_reservation(api):
    seed_receipts(api)
    deletion = deletion_module()
    with api.factory() as db:
        assert deletion.delete_billing_account_data(db, USER_A) == 2
        db.commit()
        assert deletion.delete_billing_account_data(db, USER_A) == 0
        db.commit()
    api.play.receipts[TOKEN] = subscription(USER_B)
    for user in (USER_A, USER_B):
        response = verify_http(api, user=user)
        assert response.status_code == 409
        assert response.json()["detail"] == "purchase_account_mismatch"
    assert api.play.calls == [] and api.play.ack_calls == []


def test_helper_allows_legacy_database_without_any_billing_tables(api):
    deletion = deletion_module()
    for model in (api.m.models.BillingAccount, api.m.models.BillingPurchase):
        model.__table__.drop(api.engine)
    with api.factory() as db:
        assert deletion.delete_billing_account_data(db, USER_A) == 0
        assert db.get(api.m.User, USER_A) is not None


def test_partial_billing_schema_fails_closed_with_fixed_error(api):
    deletion = deletion_module()
    api.m.models.BillingPurchase.__table__.drop(api.engine)
    with api.factory() as db:
        with pytest.raises(deletion.BillingAccountDeletionError) as failure:
            deletion.delete_billing_account_data(db, USER_A)
        assert str(failure.value) == "billing_account_deletion_unavailable"
        db.rollback()
        assert db.get(api.m.User, USER_A) is not None


def test_helper_masks_database_failure_and_does_not_commit_partial_scrub(api, monkeypatch):
    seed_receipts(api)
    deletion = deletion_module()
    with api.factory() as db:
        def failed_flush(*args, **kwargs):
            raise RuntimeError("synthetic-private-sql-and-receipt-error")
        monkeypatch.setattr(db, "flush", failed_flush)
        with pytest.raises(deletion.BillingAccountDeletionError) as failure:
            deletion.delete_billing_account_data(db, USER_A)
        assert str(failure.value) == "billing_account_deletion_unavailable"
        assert "synthetic-private" not in str(failure.value)
        db.rollback()
    with api.factory() as db:
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)).user_id == USER_A
        assert db.get(api.m.models.BillingAccount, USER_A) is not None


@pytest.fixture
def deletion_api(api, monkeypatch):
    route = importlib.import_module("app.api.endpoints.kakao_login")
    csv_cleanup, test_csv_cleanup = Mock(return_value=0), Mock(return_value=0)
    monkeypatch.setattr(route, "delete_user_labeled_csv_files", csv_cleanup)
    monkeypatch.setattr(route, "delete_test_account_csv_files", test_csv_cleanup)
    monkeypatch.setattr(route.family_group_service, "leave_family_group",
                        lambda *args, **kwargs: True)
    unlink = AsyncMock(return_value=True)
    monkeypatch.setattr(route.kakao_service, "admin_unlink_user", unlink)
    with api.factory() as db:
        db.get(api.m.User, USER_A).kakao_id = "123456789"
        db.commit()
    api.client.app.include_router(route.router, prefix="/api/auth/kakao")
    api.deletion_route, api.unlink = route, unlink
    api.csv_cleanup, api.test_csv_cleanup = csv_cleanup, test_csv_cleanup
    return api


def delete_http(api, *, actor=USER_A, user_id=USER_A):
    return api.client.request("DELETE", "/api/auth/kakao/delete",
                              json={"user_id": user_id}, headers=api.headers[actor])


def test_delete_endpoint_scrubs_and_deletes_before_external_unlink_with_checkout_off(deletion_api):
    api = deletion_api
    predecessor, other = seed_receipts(api)
    api.settings = replace(api.settings, enabled=False)

    async def inspect_committed_state(kakao_id):
        assert kakao_id == 123456789
        with api.factory() as db:
            assert db.get(api.m.User, USER_A) is None
            assert db.get(api.m.models.BillingAccount, USER_A) is None
            for token_digest in (digest(TOKEN), predecessor):
                assert_minimal_tombstone(db.get(api.m.models.BillingPurchase, token_digest))
        return True

    api.unlink.side_effect = inspect_committed_state
    assert delete_http(api).status_code == 200
    api.unlink.assert_awaited_once()
    api.csv_cleanup.assert_called_once_with(USER_A)
    api.test_csv_cleanup.assert_not_called()
    with api.factory() as db:
        assert db.get(api.m.User, USER_B).is_active
        assert db.get(api.m.models.BillingPurchase, other).user_id == USER_B
    assert delete_http(api).status_code == 401
    assert api.play.calls == [] and api.play.ack_calls == []


def test_delete_endpoint_rolls_back_account_and_billing_on_cleanup_failure(deletion_api, monkeypatch):
    api = deletion_api
    seed_receipts(api)
    deletion = deletion_module()

    def fail_after_scrub(db, user_id):
        deletion.delete_billing_account_data(db, user_id)
        raise deletion.BillingAccountDeletionError()

    monkeypatch.setattr(api.deletion_route, "delete_billing_account_data", fail_after_scrub)
    assert delete_http(api).status_code == 500
    api.unlink.assert_not_awaited()
    api.csv_cleanup.assert_not_called()
    api.test_csv_cleanup.assert_not_called()
    with api.factory() as db:
        assert db.get(api.m.User, USER_A) is not None
        assert db.get(api.m.models.BillingAccount, USER_A) is not None
        record = db.get(api.m.models.BillingPurchase, digest(TOKEN))
        assert record.user_id == USER_A and record.token_encrypted and record.active


def test_delete_endpoint_rejects_different_jwt_actor_without_scrubbing(deletion_api):
    api = deletion_api
    seed_receipts(api)
    assert delete_http(api, actor=USER_B).status_code == 403
    api.unlink.assert_not_awaited()
    with api.factory() as db:
        assert db.get(api.m.User, USER_A) is not None
        assert db.get(api.m.models.BillingPurchase, digest(TOKEN)).user_id == USER_A
    assert api.play.calls == [] and api.play.ack_calls == []
