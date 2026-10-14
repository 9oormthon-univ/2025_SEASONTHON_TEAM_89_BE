"""Offline scheduling regressions without importing production DB or credentials."""
import asyncio
import builtins
import importlib.util
from pathlib import Path
import socket
import sys
import threading
import types
from types import SimpleNamespace

from fastapi import APIRouter
import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def maintenance(monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("External connections are forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    state = SimpleNamespace(settings=SimpleNamespace(enabled=True, maintenance_enabled=True,
                                                     configuration_complete=True))
    adapter = types.ModuleType("app.services.play_billing_client")
    adapter.BillingSettings = SimpleNamespace(from_env=lambda: state.settings)
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
    module = load_module("offline_billing_maintenance", "app/services/billing_maintenance.py")
    return module, state


def forbid_runtime_imports(monkeypatch):
    attempted = []
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"app.core.database", "app.api.endpoints.billing"}:
            attempted.append(name)
            raise AssertionError("Disabled maintenance imported a billing runtime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    return attempted


def install_services(monkeypatch, state, *, retry=None, sync=None):
    sessions = []

    class Session:
        def __enter__(self):
            self.closed = False
            self.error = None
            sessions.append(self)
            return self

        def __exit__(self, kind, error, traceback):
            self.closed, self.error = True, error

    database = types.ModuleType("app.core.database")
    database.SessionLocal = Session
    routes = types.ModuleType("app.api.endpoints.billing")

    def service_for(db, settings, *, require_enabled):
        assert settings is state.settings
        assert require_enabled is False
        return SimpleNamespace(
            retry_unacknowledged=lambda: retry(db) if retry else {"checked": 0, "failed": 0},
            sync_voided=lambda: sync(db) if sync else 0,
        )

    routes.service_for = service_for
    monkeypatch.setitem(sys.modules, database.__name__, database)
    monkeypatch.setitem(sys.modules, routes.__name__, routes)
    return sessions


def test_off_never_inspects_secret_metadata_or_imports_runtime(maintenance, monkeypatch):
    module, state = maintenance

    class DisabledSettings:
        enabled = False
        maintenance_enabled = False

        @property
        def configuration_complete(self):
            pytest.fail("Disabled maintenance inspected secret-file metadata")

    state.settings = DisabledSettings()
    attempted = forbid_runtime_imports(monkeypatch)
    module.maintenance_once()
    assert attempted == []


def test_incomplete_settings_skip_runtime_without_logging_values(maintenance, monkeypatch, caplog):
    module, state = maintenance
    state.settings = SimpleNamespace(enabled=True, maintenance_enabled=True, configuration_complete=False,
                                     credentials_file="private-credential-path")
    attempted = forbid_runtime_imports(monkeypatch)
    module.maintenance_once()
    assert attempted == []
    assert "billing_maintenance_not_configured" in caplog.text
    assert "private-credential-path" not in caplog.text


def test_recovery_jobs_get_distinct_sessions_closed_each_run(maintenance, monkeypatch):
    module, state = maintenance
    calls = []
    sessions = install_services(
        monkeypatch, state,
        retry=lambda db: calls.append(("retry", db)) or {"checked": 2, "failed": 0},
        sync=lambda db: calls.append(("sync", db)) or 1,
    )
    module.maintenance_once()
    module.maintenance_once()
    assert [kind for kind, _ in calls] == ["retry", "sync", "retry", "sync"]
    assert len({id(db) for db in sessions}) == 4
    assert all(db.closed for db in sessions)


def test_pilot_recovery_runs_while_general_checkout_remains_off(maintenance, monkeypatch):
    module, state = maintenance
    state.settings = SimpleNamespace(enabled=False, maintenance_enabled=True, configuration_complete=True)
    sessions = install_services(monkeypatch, state)
    module.maintenance_once()
    assert len(sessions) == 2 and all(db.closed for db in sessions)


def test_retry_failure_does_not_block_voided_sync_or_leak_exception(maintenance, monkeypatch, caplog):
    module, state = maintenance
    calls = []

    def retry(db):
        raise RuntimeError("confidential-receipt-token")

    sessions = install_services(monkeypatch, state, retry=retry,
                                sync=lambda db: calls.append(db) or 1)
    module.maintenance_once()
    assert calls == [sessions[1]]
    assert all(db.closed for db in sessions)
    assert isinstance(sessions[0].error, RuntimeError)
    assert "billing_retry_unacknowledged_failed" in caplog.text
    assert "confidential-receipt-token" not in caplog.text


def test_partial_acknowledgement_failure_is_visible_without_receipt_data(maintenance, monkeypatch, caplog):
    module, state = maintenance
    install_services(monkeypatch, state, retry=lambda db: {"checked": 4, "failed": 2})
    module.maintenance_once()
    assert "billing_acknowledgement_retry checked=4 failed=2" in caplog.text


def test_failed_voided_sync_is_retried_on_next_run(maintenance, monkeypatch, caplog):
    module, state = maintenance
    attempts = []

    def sync(db):
        attempts.append(db)
        if len(attempts) == 1:
            raise RuntimeError("private-google-request-url")
        return 0

    sessions = install_services(monkeypatch, state, sync=sync)
    module.maintenance_once()
    module.maintenance_once()
    assert len(attempts) == 2
    assert all(db.closed for db in sessions)
    assert "billing_sync_voided_failed" in caplog.text
    assert "private-google-request-url" not in caplog.text


def test_loop_runs_blocking_work_in_worker_and_cancels_between_runs(maintenance, monkeypatch):
    module, _ = maintenance
    worker_ids, intervals = [], []

    async def exercise():
        main_id = threading.get_ident()
        waiting = asyncio.Event()
        block = asyncio.Event()

        def run_once():
            worker_ids.append(threading.get_ident())

        async def sleep(seconds):
            intervals.append(seconds)
            waiting.set()
            await block.wait()

        monkeypatch.setattr(module, "maintenance_once", run_once)
        monkeypatch.setattr(module.asyncio, "sleep", sleep)
        task = asyncio.create_task(module.billing_maintenance_loop())
        try:
            await asyncio.wait_for(waiting.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert worker_ids and all(worker_id != main_id for worker_id in worker_ids)

    asyncio.run(exercise())
    assert len(worker_ids) == 1
    assert intervals == [900]


def test_loop_survives_configuration_exception_without_leaking_it(maintenance, monkeypatch, caplog):
    module, _ = maintenance
    attempts, intervals = [], []
    original_sleep = asyncio.sleep

    async def exercise():
        complete = asyncio.Event()

        def run_once():
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("private-configuration-value")
            complete.set()

        async def sleep(seconds):
            intervals.append(seconds)
            await original_sleep(0)

        async def in_test_threadpool(operation):
            operation()

        monkeypatch.setattr(module, "maintenance_once", run_once)
        monkeypatch.setattr(module, "run_in_threadpool", in_test_threadpool)
        monkeypatch.setattr(module.asyncio, "sleep", sleep)
        task = asyncio.create_task(module.billing_maintenance_loop())
        try:
            await asyncio.wait_for(complete.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    assert len(attempts) >= 2
    assert intervals and set(intervals) == {900}
    assert "billing_maintenance_failed" in caplog.text
    assert "private-configuration-value" not in caplog.text


@pytest.mark.parametrize("training_enabled", [True, False])
def test_api_lifespan_starts_and_stops_billing_with_optional_training(monkeypatch, training_enabled):
    app_module = types.ModuleType("app")
    app_module.__path__ = [str(ROOT / "app")]
    app_module.WEB_HOST, app_module.WEB_PORT = "127.0.0.1", 10500
    api = types.ModuleType("app.api")
    api.routers = SimpleNamespace(router=APIRouter())
    endpoints = types.ModuleType("app.api.endpoints")
    endpoints.legal = SimpleNamespace(router=APIRouter())
    fraud = types.ModuleType("app.services.check_fraud")
    training = types.ModuleType("app.services.model_training")
    billing = types.ModuleType("app.services.billing_maintenance")
    started, stopped = [], []
    ready = {}

    async def start_processing():
        started.append("fraud")

    async def worker(name):
        started.append(name)
        ready[name].set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(name)

    fraud.start_processing = start_processing
    training.training_loop = lambda: worker("training")
    billing.billing_maintenance_loop = lambda: worker("billing")
    for module in (app_module, api, endpoints, fraud, training, billing):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("ML_AUTO_TRAIN", "true" if training_enabled else "false")
    module = load_module("offline_billing_lifespan", "app/__main__.py")

    async def exercise():
        ready.update(billing=asyncio.Event(), training=asyncio.Event())
        async with module.lifespan(module.app):
            await asyncio.wait_for(ready["billing"].wait(), 2)
            if training_enabled:
                await asyncio.wait_for(ready["training"].wait(), 2)

    asyncio.run(exercise())
    assert started[0] == "fraud"
    assert set(started[1:]) == ({"billing", "training"} if training_enabled else {"billing"})
    assert set(stopped) == set(started[1:])
