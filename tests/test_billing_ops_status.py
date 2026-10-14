"""Read-only operator status uses isolated SQLite and never loads Google keys."""
import builtins
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import logging
from pathlib import Path
import socket
import sys
import types
from types import SimpleNamespace

import pytest
from sqlalchemy import Boolean, Column, String, create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
USER = "00000000-0000-4000-8000-000000000001"
INACTIVE = "00000000-0000-4000-8000-000000000002"
DELETED = "00000000-0000-4000-8000-000000000003"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def status_runtime(monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("External connections are forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    base = declarative_base()

    class User(base):
        __tablename__ = "users"
        user_id = Column(String(36), primary_key=True)
        is_active = Column(Boolean, nullable=False)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    factory = sessionmaker(bind=engine)
    database = types.ModuleType("app.core.database")
    database.Base, database.SessionLocal = base, factory
    users = types.ModuleType("app.models.user")
    users.User = User
    monkeypatch.setitem(sys.modules, database.__name__, database)
    monkeypatch.setitem(sys.modules, users.__name__, users)
    models = load_module("offline_status_billing_models", "app/models/play_billing.py")
    monkeypatch.setitem(sys.modules, "app.models.play_billing", models)
    base.metadata.create_all(engine)
    with factory() as db:
        db.add_all([User(user_id=USER, is_active=True), User(user_id=INACTIVE, is_active=False)])
        db.commit()
    adapter = types.ModuleType("app.services.play_billing_client")
    adapter.PRODUCTS = {
        "ad_free_monthly": SimpleNamespace(product_type="subs"),
        "ad_free_yearly": SimpleNamespace(product_type="subs"),
        "ad_free_lifetime": SimpleNamespace(product_type="inapp"),
    }
    settings = SimpleNamespace(enabled=False, test_user_ids=(USER,),
                               credentials_file="private-service-account-file",
                               token_key_file="private-encryption-key-file")
    adapter.BillingSettings = SimpleNamespace(from_env=lambda: settings)
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
    ops = load_module("offline_status_billing_ops", "app/billing_ops.py")
    runtime = SimpleNamespace(ops=ops, models=models, database=database, factory=factory,
                              engine=engine, settings=settings)
    try:
        yield runtime
    finally:
        engine.dispose()


def purchase(runtime, name, **overrides):
    values = dict(token_sha256=name.ljust(64, "x"), user_id=USER, token_encrypted="synthetic-ciphertext",
                  product_id="ad_free_lifetime", product_type="inapp", active=False, acknowledged=False,
                  expires_at=None, checked_at=None)
    values.update(overrides)
    return runtime.models.BillingPurchase(**values)


def test_status_without_history_reports_counts_only_and_no_key_runtime(status_runtime, monkeypatch):
    r = status_runtime
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"app.services.play_billing_service", "app.api.endpoints.billing"}:
            pytest.fail("Read-only status imported the key/Google runtime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert r.ops.status_snapshot(r.settings, NOW) == {
        "ok": True, "checkout_enabled": False, "test_account_count": 1,
        "schema_ready": True, "voided_last_success": None, "unacknowledged_purchases": 0,
    }


def test_status_counts_only_recoverable_receipts_and_does_not_write(status_runtime):
    r = status_runtime
    with r.factory() as db:
        db.add(r.models.BillingSyncState(name="voided", checked_until=NOW - timedelta(minutes=5)))
        db.add_all([
            purchase(r, "valid-lifetime"),
            purchase(r, "valid-subscription", product_id="ad_free_monthly", product_type="subs",
                     expires_at=NOW + timedelta(hours=1)),
            purchase(r, "expired-subscription", product_id="ad_free_yearly", product_type="subs",
                     expires_at=NOW - timedelta(seconds=1)),
            purchase(r, "subscription-expiry-boundary", product_id="ad_free_monthly", product_type="subs",
                     expires_at=NOW),
            purchase(r, "subscription-without-expiry", product_id="ad_free_monthly", product_type="subs"),
            purchase(r, "lifetime-with-expiry", expires_at=NOW + timedelta(days=1)),
            purchase(r, "unknown-product", product_id="unknown-product"),
            purchase(r, "wrong-type", product_id="ad_free_monthly"),
            purchase(r, "inactive-user", user_id=INACTIVE),
            purchase(r, "deleted-owner", user_id=DELETED),
            purchase(r, "revoked", revoked_at=NOW),
            purchase(r, "superseded", superseded_by_hash="new-receipt-hash"),
            purchase(r, "acknowledged", acknowledged=True),
            purchase(r, "empty-encrypted-token", token_encrypted=""),
        ])
        db.commit()
    statements = []
    event.listen(r.engine, "after_cursor_execute",
                 lambda connection, cursor, statement, parameters, context, many: statements.append(statement))
    result = r.ops.status_snapshot(r.settings, NOW)
    assert result["unacknowledged_purchases"] == 2
    assert result["voided_last_success"] == "2026-10-14T23:55:00Z"
    assert all(statement.lstrip().split()[0].upper() in {"SELECT", "PRAGMA"} for statement in statements)
    serialized = json.dumps(result)
    assert USER not in serialized and "synthetic-ciphertext" not in serialized


def test_status_missing_schema_reports_allowlisted_table_names(status_runtime):
    r = status_runtime
    r.models.BillingPurchase.__table__.drop(r.engine)
    result = r.ops.status_snapshot(r.settings, NOW)
    assert result["ok"] is False and result["schema_ready"] is False
    assert result["stage"] == "schema" and result["reason"] == "billing_tables_missing"
    assert result["missing_tables"] == ["billing_purchases"]


def test_status_database_error_and_logging_never_expose_connection_details(status_runtime, caplog, monkeypatch):
    r = status_runtime

    def fail():
        logging.getLogger("private.sql").critical("private-database-password")
        raise RuntimeError("private-database-password")

    monkeypatch.setattr(r.database, "SessionLocal", fail)
    result = r.ops.status_snapshot(r.settings, NOW)
    assert result["stage"] == "database" and result["reason"] == "database_unavailable"
    assert "private-database-password" not in json.dumps(result) + caplog.text


def test_status_query_failure_is_safe_and_session_closed(status_runtime, monkeypatch):
    r = status_runtime
    sessions = []

    class FailedSession:
        def __enter__(self):
            sessions.append(self)
            self.closed = False
            return self

        def __exit__(self, *args):
            self.closed = True

        def connection(self):
            return None

        def get_bind(self):
            return r.engine

        def get(self, *args):
            raise RuntimeError("private-query-values")

    monkeypatch.setattr(r.database, "SessionLocal", FailedSession)
    result = r.ops.status_snapshot(r.settings, NOW)
    assert result["stage"] == "status" and result["reason"] == "billing_status_check_failed"
    assert sessions[0].closed and "private-query-values" not in json.dumps(result)


def test_status_suppresses_sql_echo_and_restores_existing_logging_threshold(status_runtime, capsys):
    r = status_runtime
    r.engine.echo = True
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.ERROR)
        assert r.ops.status_snapshot(r.settings, NOW)["ok"]
        assert logging.root.manager.disable == logging.ERROR
    finally:
        logging.disable(previous)
    assert "SELECT" not in capsys.readouterr().out


def test_status_operator_command_returns_json_without_loading_maintenance(status_runtime, monkeypatch, capsys):
    r = status_runtime
    monkeypatch.setattr(sys, "argv", ["billing_ops", "status"])
    r.ops.main()
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["test_account_count"] == 1
    assert USER not in json.dumps(result)


def test_status_operator_command_exits_nonzero_for_missing_schema(status_runtime, monkeypatch, capsys):
    r = status_runtime
    r.models.BillingSyncState.__table__.drop(r.engine)
    monkeypatch.setattr(sys, "argv", ["billing_ops", "status"])
    with pytest.raises(SystemExit) as raised:
        r.ops.main()
    assert raised.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False and result["reason"] == "billing_tables_missing"
