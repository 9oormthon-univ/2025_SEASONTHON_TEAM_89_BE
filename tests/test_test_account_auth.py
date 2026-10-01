"""Isolated API regressions for review accounts; never import the production DB/lifespan.

Only SQLite in-memory data, temporary CSV directories and recording push doubles are
used. Network calls fail closed even if a future route accidentally bypasses a double.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import socket
import sqlite3
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool


# Deliberately public fixture credentials, not deployment credentials.
CODE_A = "qa_alpha_0123456789abcdef"
CODE_B = "qa_bravo_0123456789abcdef"
WRONG_CODE = "qa_wrong_0123456789abcdef"
HASH_A = hashlib.sha256(CODE_A.encode("ascii")).hexdigest()
HASH_B = hashlib.sha256(CODE_B.encode("ascii")).hexdigest()
CSV_BODY = (
    "text,label,engine_verdict,patterns,score,source,app,timestamp\n"
    "테스트 계정 합성 문장,위험,위험,,0.99,manual,test,2026-10-01T12:00:00Z\n"
).encode("utf-8-sig")


@pytest.fixture(scope="module")
def isolated_modules():
    patch = pytest.MonkeyPatch()
    original_app_modules = {name: module for name, module in sys.modules.items()
                            if name == "app" or name.startswith("app.")}
    patch.setenv("ENVIRONMENT", "test")
    patch.setenv("JWT_SECRET_KEY", "isolated-qa-secret-not-valid-for-any-deployment-2026")
    patch.setenv("AUTH_KEY_PATH", "")
    patch.setenv("FIREBASE_CREDENTIALS_PATH", "")
    patch.setenv("KAKAO_ADMIN_KEY", "")
    runtime = SimpleNamespace(factory=None)
    base = declarative_base()

    def get_db():
        assert runtime.factory is not None, "SQLite test session must be initialized"
        session = runtime.factory()
        try:
            yield session
        finally:
            session.close()

    # Replace the DB module BEFORE any repository/service import. Its normal
    # import constructs a MySQL engine against the deployment host.
    database = types.ModuleType("app.core.database")
    database.Base = base
    database.get_db = get_db
    database.SessionLocal = lambda: runtime.factory()
    database.engine = None
    patch.setitem(sys.modules, "app.core.database", database)

    app_module = importlib.import_module("app")
    patch.setattr(app_module, "AUTH_KEY_PATH", "")
    patch.setattr(app_module, "FIREBASE_CREDENTIALS_PATH", "")
    fcm = importlib.import_module("app.services.fcm_pushalarm")
    patch.setattr(fcm.fcm_pusher, "init", lambda: None)
    modules = SimpleNamespace(
        auth=importlib.import_module("app.api.endpoints.auth"),
        family=importlib.import_module("app.api.endpoints.family_group"),
        notifications=importlib.import_module("app.api.endpoints.notifications"),
        kakao=importlib.import_module("app.api.endpoints.kakao_login"),
        ml=importlib.import_module("app.api.endpoints.ml_data"),
        service=importlib.import_module("app.services.test_account_service"),
        boundary=importlib.import_module("app.services.test_account_boundary"),
        storage=importlib.import_module("app.services.ml_data_storage"),
        jwt=importlib.import_module("app.services.jwt_service").jwt_service,
        User=importlib.import_module("app.models.user").User,
        base=base, runtime=runtime, get_db=get_db, fcm=fcm,
    )
    try:
        yield modules
    finally:
        runtime.factory = None
        patch.undo()
        # Do not leave test Settings or SQLite dependencies in another test's imports.
        for name in list(sys.modules):
            if (name == "app" or name.startswith("app.")) and name not in original_app_modules:
                del sys.modules[name]
        sys.modules.update(original_app_modules)


def _create_group_tables(engine):
    statements = [
        """CREATE TABLE family_groups (
            id TEXT PRIMARY KEY, group_name TEXT NOT NULL, creator_id TEXT NOT NULL,
            join_code TEXT NOT NULL UNIQUE, created_at TIMESTAMP NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT 1, current_members INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
            user_id TEXT NOT NULL UNIQUE, nickname TEXT NOT NULL,
            is_creator BOOLEAN NOT NULL DEFAULT 0, joined_at TIMESTAMP NOT NULL)""",
        """CREATE TRIGGER count_member_insert AFTER INSERT ON group_members BEGIN
            UPDATE family_groups SET current_members = current_members + 1 WHERE id = NEW.group_id;
            END""",
        """CREATE TRIGGER count_member_delete AFTER DELETE ON group_members BEGIN
            UPDATE family_groups SET current_members = current_members - 1 WHERE id = OLD.group_id;
            END""",
        """CREATE TABLE notification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, from_user_id TEXT NOT NULL,
            to_user_id TEXT NOT NULL, group_id TEXT NOT NULL,
            notification_type TEXT NOT NULL, message TEXT, success BOOLEAN,
            sent_at DATETIME NOT NULL)""",
    ]
    for kind in ("danger", "warning"):
        statements.append(f"""CREATE TABLE {kind}_notification_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            target_user_id TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, target_user_id))""")
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


@pytest.fixture
def api(isolated_modules, monkeypatch, tmp_path):
    m = isolated_modules
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False, "detect_types": sqlite3.PARSE_DECLTYPES},
                           poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def sqlite_now(connection, _):
        connection.create_function("NOW", 0, lambda: datetime.utcnow().isoformat(" "))

    m.base.metadata.create_all(engine)
    _create_group_tables(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    m.runtime.factory = factory
    monkeypatch.setenv("TEST_ACCOUNT_CODES_JSON", json.dumps({"alpha": HASH_A, "bravo": HASH_B}))
    monkeypatch.setattr(m.auth, "attempt_limiter", m.service.AttemptLimiter())
    production_inbox, test_inbox = tmp_path / "production", tmp_path / "test"
    production_inbox.mkdir()
    test_inbox.mkdir()
    monkeypatch.setattr(m.storage, "ML_INBOX_DIR", production_inbox)
    monkeypatch.setenv("TEST_ACCOUNT_CSV_DIR", str(test_inbox))
    # These original functions capture their default inbox at import time. Wrap
    # the endpoint's regular-user storage helpers explicitly to prevent that default
    # from touching any real directory during the coexistence tests.
    save_regular = m.storage.save_user_labeled_csv
    delete_regular = m.storage.delete_user_labeled_csv_files
    monkeypatch.setattr(m.ml, "save_user_labeled_csv",
                        lambda uid, body: save_regular(uid, body, production_inbox))
    monkeypatch.setattr(m.kakao, "delete_user_labeled_csv_files",
                        lambda uid: delete_regular(uid, production_inbox))
    pushes, self_pushes, network = [], [], []

    async def record_push(*args, **kwargs):
        pushes.append(kwargs.copy() if kwargs else {"device_token": args[0]})
        return True

    async def record_self(device_token, body, level):
        self_pushes.append({"device_token": device_token, "level": level})
        return True

    def deny_connect(*args, **kwargs):
        network.append("socket")
        raise AssertionError("Network connections are forbidden in isolated auth QA")

    async def deny_http(*args, **kwargs):
        network.append("httpx")
        raise AssertionError("External HTTP is forbidden in isolated auth QA")

    notification_service = m.notifications.notification_service
    monkeypatch.setattr(notification_service, "db_dependency", m.get_db)
    monkeypatch.setattr(m.family.family_group_service, "db_dependency", m.get_db)
    monkeypatch.setattr(notification_service, "_send_push_notification", record_push)
    monkeypatch.setattr(notification_service, "_send_self_push", record_self)
    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    monkeypatch.setattr(httpx.AsyncClient, "send", deny_http)
    unlink = AsyncMock(side_effect=AssertionError("Test accounts must not invoke Kakao unlink"))
    firebase_send = AsyncMock(side_effect=AssertionError("Live Firebase must never be used"))
    monkeypatch.setattr(m.kakao.kakao_service, "admin_unlink_user", unlink)
    monkeypatch.setattr(m.fcm.fcm_pusher, "send_notification", firebase_send)
    test_app = FastAPI()
    test_app.include_router(m.auth.router, prefix="/api/auth")
    test_app.include_router(m.kakao.router, prefix="/api/auth/kakao")
    test_app.include_router(m.family.router, prefix="/api/family_group")
    test_app.include_router(m.notifications.router, prefix="/api/notifications")
    test_app.include_router(m.ml.router, prefix="/api/ml")
    with TestClient(test_app, raise_server_exceptions=False) as client:
        state = SimpleNamespace(client=client, m=m, factory=factory, engine=engine,
                                pushes=pushes, self_pushes=self_pushes, unlink=unlink,
                                production=production_inbox, test_inbox=test_inbox)
        try:
            yield state
        finally:
            assert network == [], "A test route attempted an external network operation"
            firebase_send.assert_not_called()
    m.runtime.factory = None
    engine.dispose()


def _login(api, code=CODE_A, **extra):
    response = api.client.post("/api/auth/test-account", json={"code": code, **extra})
    assert response.status_code == 200, response.text
    session = response.json()
    session["headers"] = {"Authorization": "Bearer " + session["access_token"]}
    return session


def _regular(api, *, token="regular-device-token:qa", group=None):
    uid = str(uuid4())
    with api.factory() as db:
        db.add(api.m.User(user_id=uid, kakao_id=str(uuid4().int), nickname="일반 사용자",
                          warning_count=7, danger_count=4, device_token=token,
                          group_id=group, is_active=True))
        db.commit()
    jwt = api.m.jwt.create_access_token({"user_id": uid, "nickname": "일반 사용자"})
    return uid, {"Authorization": "Bearer " + jwt}


def _row(api, uid):
    with api.factory() as db:
        return db.execute(text("SELECT * FROM users WHERE user_id=:uid"), {"uid": uid}).mappings().first()


def _query(api, statement, **params):
    with api.factory() as db:
        return db.execute(text(statement), params).mappings().all()


def _execute(api, statement, **params):
    with api.factory() as db:
        db.execute(text(statement), params)
        db.commit()


def _family(api, session):
    response = api.client.get("/api/family_group/info/" + session["user"]["user_id"],
                              headers=session["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def test_login_issues_ordinary_bearer_and_only_synthetic_family(api, caplog):
    session = _login(api)
    uid = session["user"]["user_id"]
    assert session["auth_provider"] == "test"
    assert session["token_type"] == "bearer"
    assert session["is_new_user"] is True
    claims = api.m.jwt.verify_token(session["access_token"])
    assert claims["user_id"] == uid
    assert claims["auth_provider"] == "test"
    assert claims["type"] == "access"
    assert {"iat", "exp"} <= claims.keys()
    assert not ({"admin", "is_admin", "role", "scope"} & claims.keys())
    family = _family(api, session)
    assert family["member_count"] == 2
    assert family["creator_id"] == uid
    assert {member["user_id"] for member in family["members"]} == {
        row["user_id"] for row in _query(api, "SELECT user_id FROM users")}
    assert _row(api, uid)["kakao_id"] == "test-account:alpha"
    assert (_row(api, uid)["warning_count"], _row(api, uid)["danger_count"]) == (0, 0)
    fixture = next(row for row in _query(api, "SELECT * FROM users") if row["user_id"] != uid)
    assert fixture["kakao_id"] == "test-account:fixture:alpha"
    assert fixture["device_token"] is None
    assert (fixture["warning_count"], fixture["danger_count"]) == (2, 1)
    assert api.pushes == api.self_pushes == []
    assert CODE_A not in json.dumps(session) and HASH_A not in json.dumps(session)
    assert CODE_A not in caplog.text and HASH_A not in caplog.text
    assert api.client.get("/api/auth/kakao/me", headers=session["headers"]).status_code == 200


def test_seed_member_count_is_exact_with_or_without_triggers_and_capacity_guard_works(api):
    alpha = _login(api)
    alpha_family = _family(api, alpha)
    # The existing INSERT triggers already counted two members; normalization must not add two again.
    assert _query(api, "SELECT current_members FROM family_groups WHERE id=:gid",
                  gid=alpha_family["group_id"])[0]["current_members"] == 2
    _execute(api, "DROP TRIGGER count_member_insert")
    _execute(api, "DROP TRIGGER count_member_delete")
    bravo = _login(api, CODE_B)
    bravo_family = _family(api, bravo)
    assert bravo_family["member_count"] == 2
    assert _query(api, "SELECT current_members FROM family_groups WHERE id=:gid",
                  gid=bravo_family["group_id"])[0]["current_members"] == 2
    assert _family(api, _login(api, CODE_B))["member_count"] == 2
    bravo_uid = bravo["user"]["user_id"]
    assert api.client.delete("/api/family_group/leave/" + bravo_uid,
                             headers=bravo["headers"]).status_code == 200
    _execute(api, "UPDATE family_groups SET current_members=8 WHERE id=:gid", gid=alpha_family["group_id"])
    response = api.client.post("/api/family_group/join", headers=bravo["headers"],
                               json={"user_id": bravo_uid, "join_code": alpha_family["join_code"],
                                     "nickname": "테스트"})
    assert response.status_code == 409
    assert _row(api, bravo_uid)["group_id"] is None
    assert _family(api, alpha)["member_count"] == 2


def test_relogin_preserves_counts_and_does_not_reseed_a_left_group(api):
    first = _login(api, device_token="test-device:alpha")
    uid = first["user"]["user_id"]
    _execute(api, "UPDATE users SET warning_count=9,danger_count=8 WHERE user_id=:uid", uid=uid)
    assert api.client.delete("/api/family_group/leave/" + uid, headers=first["headers"]).status_code == 200
    second = _login(api)
    assert second["user"]["user_id"] == uid
    assert second["is_new_user"] is False
    assert second["user"]["group_id"] is None
    assert (second["user"]["warning_count"], second["user"]["danger_count"]) == (9, 8)
    assert _row(api, uid)["device_token"] == "test-device:alpha"
    assert _query(api, "SELECT * FROM family_groups") == []
    assert len(_query(api, "SELECT * FROM users")) == 2


@pytest.mark.parametrize("bad_config", [
    None, "", "not-json", "[]", "{}", '{"alpha": "plaintext-code"}',
    json.dumps({"../bad": HASH_A}), json.dumps({"alpha": HASH_A.upper()}),
    json.dumps({"alpha": HASH_A, "bravo": HASH_A}),
    '{"alpha":"' + HASH_A + '","alpha":"' + HASH_B + '"}',
    json.dumps({f"slot{i}": hashlib.sha256(str(i).encode()).hexdigest() for i in range(9)}),
    " " * 2049,
])
def test_missing_or_malformed_configuration_is_disabled_and_creates_nothing(api, monkeypatch, bad_config):
    if bad_config is None:
        monkeypatch.delenv("TEST_ACCOUNT_CODES_JSON", raising=False)
    else:
        monkeypatch.setenv("TEST_ACCOUNT_CODES_JSON", bad_config)
    response = api.client.post("/api/auth/test-account", json={"code": CODE_A})
    assert response.status_code == 503
    assert CODE_A not in response.text and HASH_A not in response.text
    assert _query(api, "SELECT * FROM users") == []


@pytest.mark.parametrize("body", [
    "not-json", "[]", "null", "{}", '{"code":123}', '{"code":"short"}',
    json.dumps({"code": CODE_A, "admin": True}),
    json.dumps({"code": CODE_A, "device_token": {"bad": "token"}}),
    json.dumps({"code": CODE_A, "device_token": "token with whitespace"}),
    json.dumps({"code": CODE_A, "device_token": "x" * 256}),
    json.dumps({"code": "x" * 129}),
    '{"code":"' + CODE_A + '","code":"' + CODE_A + '"}',
    '{"code":"' + CODE_A + '","unused":"' + "x" * 2050 + '"}',
    b'\xff\xfe',
])
def test_bad_request_body_is_sanitized_without_echoing_credentials(api, body, caplog):
    response = api.client.post("/api/auth/test-account", content=body,
                               headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json() == {"detail": "테스트 로그인 입력 형식이 올바르지 않습니다."}
    assert CODE_A not in response.text and CODE_A not in caplog.text
    assert _query(api, "SELECT * FROM users") == []


def test_valid_shaped_wrong_code_fails_401_without_database_or_credential_leak(api, caplog):
    response = api.client.post("/api/auth/test-account", json={"code": WRONG_CODE})
    assert response.status_code == 401
    assert WRONG_CODE not in response.text and WRONG_CODE not in caplog.text
    assert _query(api, "SELECT * FROM users") == []


def test_transaction_failure_rolls_back_and_does_not_log_raw_database_credentials(api, monkeypatch, caplog):
    def fail_after_insert(db, slot, device_token=None):
        db.add(api.m.User(user_id=str(uuid4()), kakao_id="test-account:failed", nickname="격리 실패 검증"))
        db.flush()
        raise RuntimeError("database parameters included " + CODE_A)

    monkeypatch.setattr(api.m.auth, "login_test_account", fail_after_insert)
    response = api.client.post("/api/auth/test-account", json={"code": CODE_A})
    assert response.status_code == 503
    assert CODE_A not in response.text and CODE_A not in caplog.text
    assert _query(api, "SELECT * FROM users") == []


def test_rate_limit_counts_successes_and_failures_and_ignores_forwarded_peer(api):
    _login(api)
    for attempt in range(9):
        response = api.client.post("/api/auth/test-account", json={"code": WRONG_CODE},
                                   headers={"X-Forwarded-For": f"192.0.2.{attempt}"})
        assert response.status_code == 401
    response = api.client.post("/api/auth/test-account", json={"code": CODE_A},
                               headers={"X-Forwarded-For": "192.0.2.255"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "300"


def test_limiter_has_global_cap_memory_cap_and_window_expiry(isolated_modules):
    now = [1000.0]
    limiter = isolated_modules.service.AttemptLimiter(per_client=10, global_limit=3,
                                                     max_clients=2, clock=lambda: now[0])
    limiter.check("peer-a")
    limiter.check("peer-b")
    with pytest.raises(HTTPException) as memory_full:
        limiter.check("peer-c")
    assert memory_full.value.status_code == 429
    limiter.check("peer-a")
    with pytest.raises(HTTPException) as global_full:
        limiter.check("peer-b")
    assert global_full.value.status_code == 429
    assert len(limiter.clients) == 2
    assert all(isinstance(key, bytes) for key in limiter.clients)
    now[0] += 300
    limiter.check("peer-c")
    assert len(limiter.total) == 1 and len(limiter.clients) == 1


@pytest.mark.parametrize("disabled", [None, "{}", "not-json", json.dumps({"bravo": HASH_B})])
def test_disabling_or_removing_slot_revokes_existing_access_jwt(api, monkeypatch, disabled):
    session = _login(api)
    if disabled is None:
        monkeypatch.delenv("TEST_ACCOUNT_CODES_JSON", raising=False)
    else:
        monkeypatch.setenv("TEST_ACCOUNT_CODES_JSON", disabled)
    response = api.client.get("/api/auth/kakao/me", headers=session["headers"])
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_inactive_test_user_is_not_reactivated_and_its_jwt_is_rejected(api):
    session = _login(api)
    uid = session["user"]["user_id"]
    _execute(api, "UPDATE users SET is_active=0 WHERE user_id=:uid", uid=uid)
    assert api.client.post("/api/auth/test-account", json={"code": CODE_A}).status_code == 401
    assert api.client.get("/api/auth/kakao/me", headers=session["headers"]).status_code == 401
    assert _row(api, uid)["is_active"] == 0


def test_test_jwt_cannot_impersonate_real_user_at_any_user_id_endpoint(api):
    session = _login(api)
    regular_uid, _ = _regular(api)
    original = dict(_row(api, regular_uid))
    calls = [
        ("get", "/api/family_group/info/" + regular_uid, {}),
        ("get", "/api/notifications/settings/" + regular_uid, {}),
        ("delete", "/api/family_group/leave/" + regular_uid, {}),
        ("post", "/api/auth/device-token", {"json": {"user_id": regular_uid, "device_token": "evil:test"}}),
        ("post", "/api/family_group/create", {"json": {"user_id": regular_uid, "group_name": "악용", "nickname": "악용"}}),
        ("post", "/api/family_group/join", {"json": {"user_id": regular_uid, "join_code": "ABCDEFGHIJ", "nickname": "악용"}}),
        ("post", "/api/family_group/kick", {"json": {"creator_id": regular_uid, "target_user_id": session["user"]["user_id"]}}),
        ("post", "/api/notifications/setting", {"json": {"user_id": regular_uid, "target_user_id": session["user"]["user_id"], "enabled": False}}),
        ("post", "/api/notifications/danger", {"json": {"from_user_id": regular_uid, "danger_type": "test"}}),
        ("put", "/api/notifications/danger-count", {"json": {"user_id": regular_uid, "danger_count": 999}}),
        ("put", "/api/notifications/warning-count", {"json": {"user_id": regular_uid, "warning_count": 999}}),
        ("post", "/api/ml/labeled-csv?user_id=" + regular_uid, {"content": CSV_BODY}),
        ("delete", "/api/auth/kakao/delete", {"json": {"user_id": regular_uid}}),
    ]
    for method, path, options in calls:
        response = api.client.request(method, path, headers=session["headers"], **options)
        assert response.status_code == 403, (method, path, response.text)
    assert dict(_row(api, regular_uid)) == original
    assert api.pushes == api.self_pushes == []
    assert list(api.production.iterdir()) == list(api.test_inbox.iterdir()) == []
    api.unlink.assert_not_called()


def test_fixture_only_member_cannot_be_a_regular_login_subject(api):
    session = _login(api)
    fixture_uid = next(member["user_id"] for member in _family(api, session)["members"]
                       if member["user_id"] != session["user"]["user_id"])
    token = api.m.jwt.create_access_token({"user_id": fixture_uid, "auth_provider": "test"})
    assert api.client.get("/api/auth/kakao/me", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_test_and_real_families_cannot_verify_or_join_each_other(api):
    session = _login(api)
    family = _family(api, session)
    regular_uid, headers = _regular(api)
    response = api.client.post("/api/family_group/verify", headers=headers, json={"join_code": family["join_code"]})
    assert response.status_code == 200 and response.json()["is_valid"] is False
    response = api.client.post("/api/family_group/join", headers=headers,
                               json={"user_id": regular_uid, "join_code": family["join_code"], "nickname": "일반"})
    assert response.status_code == 403
    assert _row(api, regular_uid)["group_id"] is None
    real_family = api.client.post("/api/family_group/create", headers=headers,
                                  json={"user_id": regular_uid, "group_name": "실제 가족", "nickname": "일반"})
    assert real_family.status_code == 201, real_family.text
    real_code = real_family.json()["join_code"]
    response = api.client.post("/api/family_group/verify", headers=session["headers"], json={"join_code": real_code})
    assert response.status_code == 200 and response.json()["is_valid"] is False
    uid = session["user"]["user_id"]
    assert api.client.delete("/api/family_group/leave/" + uid, headers=session["headers"]).status_code == 200
    response = api.client.post("/api/family_group/join", headers=session["headers"],
                               json={"user_id": uid, "join_code": real_code, "nickname": "테스트"})
    assert response.status_code == 403
    assert _row(api, uid)["group_id"] is None
    assert len(_query(api, "SELECT * FROM group_members WHERE group_id=:gid", gid=real_family.json()["group_id"])) == 1


def test_test_actor_cannot_kick_or_change_notification_for_regular_target(api):
    alpha = _login(api)
    uid = alpha["user"]["user_id"]
    real_uid, _ = _regular(api)
    before = dict(_row(api, real_uid))
    response = api.client.post("/api/family_group/kick", headers=alpha["headers"],
                               json={"creator_id": uid, "target_user_id": real_uid})
    assert response.status_code == 403
    response = api.client.post("/api/notifications/setting", headers=alpha["headers"],
                               json={"user_id": uid, "target_user_id": real_uid, "enabled": False})
    assert response.status_code >= 400
    assert dict(_row(api, real_uid)) == before
    assert _query(api, "SELECT * FROM danger_notification_settings") == []


def test_test_family_setting_kick_create_join_and_device_token_work_normally(api):
    alpha, bravo = _login(api), _login(api, CODE_B)
    alpha_uid, bravo_uid = alpha["user"]["user_id"], bravo["user"]["user_id"]
    family = _family(api, alpha)
    fixture_uid = next(m["user_id"] for m in family["members"] if m["user_id"] != alpha_uid)
    response = api.client.post("/api/notifications/setting", headers=alpha["headers"],
                               json={"user_id": alpha_uid, "target_user_id": fixture_uid, "enabled": False})
    assert response.status_code == 200, response.text
    settings = api.client.get("/api/notifications/settings/" + alpha_uid, headers=alpha["headers"])
    assert settings.status_code == 200 and settings.json()["data"][0]["danger_enabled"] is False
    response = api.client.post("/api/family_group/kick", headers=alpha["headers"],
                               json={"creator_id": alpha_uid, "target_user_id": fixture_uid})
    assert response.status_code == 200, response.text
    assert _row(api, fixture_uid)["group_id"] is None
    assert _family(api, alpha)["member_count"] == 1
    assert api.client.delete("/api/family_group/leave/" + bravo_uid, headers=bravo["headers"]).status_code == 200
    response = api.client.post("/api/family_group/join", headers=bravo["headers"],
                               json={"user_id": bravo_uid, "join_code": family["join_code"], "nickname": "테스트 B"})
    assert response.status_code == 200, response.text
    assert _family(api, alpha)["member_count"] == 2
    response = api.client.post("/api/auth/device-token", headers=alpha["headers"],
                               json={"user_id": alpha_uid, "device_token": "qa-new-token:alpha"})
    assert response.status_code == 200 and _row(api, alpha_uid)["device_token"] == "qa-new-token:alpha"
    # Re-login must leave this user in their selected (not freshly seeded) family.
    assert _login(api, CODE_B)["user"]["group_id"] == family["group_id"]
    assert api.client.delete("/api/family_group/leave/" + alpha_uid, headers=alpha["headers"]).status_code == 200
    response = api.client.post("/api/family_group/create", headers=alpha["headers"],
                               json={"user_id": alpha_uid, "group_name": "새 테스트 가족", "nickname": "테스트 A"})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("corruption", ["member", "stale_user_group", "stale_membership", "creator"])
def test_mixed_legacy_group_fails_closed_without_touching_real_user(api, corruption):
    session = _login(api)
    uid = session["user"]["user_id"]
    family = _family(api, session)
    regular_uid, _ = _regular(api)
    if corruption == "creator":
        _execute(api, "UPDATE family_groups SET creator_id=:real WHERE id=:gid", real=regular_uid, gid=family["group_id"])
    else:
        if corruption != "stale_membership":
            _execute(api, "UPDATE users SET group_id=:gid WHERE user_id=:real", gid=family["group_id"], real=regular_uid)
        else:
            # Actor has no users.group_id but still has a corrupt legacy membership.
            _execute(api, "UPDATE users SET group_id=NULL WHERE user_id=:uid", uid=uid)
        if corruption in {"member", "stale_membership"}:
            _execute(api, "INSERT INTO group_members(group_id,user_id,nickname,joined_at) VALUES(:gid,:real,'일반',CURRENT_TIMESTAMP)",
                     gid=family["group_id"], real=regular_uid)
    original_real, original_test = dict(_row(api, regular_uid)), dict(_row(api, uid))
    calls = [
        ("get", "/api/family_group/info/" + uid, {}),
        ("delete", "/api/family_group/leave/" + uid, {}),
        ("post", "/api/family_group/kick", {"json": {"creator_id": uid, "target_user_id": regular_uid}}),
        ("post", "/api/notifications/setting", {"json": {"user_id": uid, "target_user_id": regular_uid, "enabled": False}}),
        ("post", "/api/notifications/danger", {"json": {"from_user_id": uid, "danger_type": "test"}}),
        ("put", "/api/notifications/danger-count", {"json": {"user_id": uid, "danger_count": 3}}),
        ("put", "/api/notifications/warning-count", {"json": {"user_id": uid, "warning_count": 9}}),
        ("delete", "/api/auth/kakao/delete", {"json": {"user_id": uid}}),
    ]
    for method, path, options in calls:
        response = api.client.request(method, path, headers=session["headers"], **options)
        assert response.status_code >= 400, (method, path, response.text)
    assert dict(_row(api, regular_uid)) == original_real
    assert dict(_row(api, uid)) == original_test
    assert api.pushes == api.self_pushes == []
    api.unlink.assert_not_called()


def test_danger_pushes_go_only_to_test_token_owners_and_respect_settings(api):
    alpha = _login(api, device_token="qa-test-device:alpha")
    bravo = _login(api, CODE_B, device_token="qa-test-device:bravo")
    uid, target = alpha["user"]["user_id"], bravo["user"]["user_id"]
    family = _family(api, alpha)
    assert api.client.delete("/api/family_group/leave/" + target, headers=bravo["headers"]).status_code == 200
    assert api.client.post("/api/family_group/join", headers=bravo["headers"],
                           json={"user_id": target, "join_code": family["join_code"], "nickname": "테스트 B"}).status_code == 200
    # A colliding token on a real account must prevent delivery to that physical device.
    regular_uid, _ = _regular(api, token="qa-test-device:bravo")
    danger = {"from_user_id": uid, "danger_type": "test"}
    response = api.client.post("/api/notifications/danger", headers=alpha["headers"], json=danger)
    assert response.status_code == 200, response.text
    assert response.json()["sent_count"] == 0 and api.pushes == []
    _execute(api, "UPDATE users SET device_token='qa-real-device:unique' WHERE user_id=:uid", uid=regular_uid)
    response = api.client.post("/api/notifications/danger", headers=alpha["headers"], json=danger)
    assert response.status_code == 200 and response.json()["sent_count"] == 1
    assert [push["device_token"] for push in api.pushes] == ["qa-test-device:bravo"]
    assert all(push["device_token"] == "qa-test-device:alpha" for push in api.self_pushes)
    assert api.client.post("/api/notifications/setting", headers=bravo["headers"],
                           json={"user_id": target, "target_user_id": uid, "enabled": False}).status_code == 200
    response = api.client.post("/api/notifications/danger", headers=alpha["headers"], json=danger)
    assert response.status_code == 200 and response.json()["sent_count"] == 0
    assert len(api.pushes) == 1
    assert _row(api, regular_uid)["danger_count"] == 4


def test_colliding_sender_token_is_not_self_pushed_to_real_account(api):
    alpha = _login(api, device_token="qa-shared-physical-device")
    _regular(api, token="qa-shared-physical-device")
    response = api.client.post("/api/notifications/danger", headers=alpha["headers"],
                               json={"from_user_id": alpha["user"]["user_id"], "danger_type": "test"})
    assert response.status_code == 200
    assert api.pushes == api.self_pushes == []


def test_real_actor_push_cannot_reach_a_token_also_owned_by_test_account(api):
    alpha = _login(api, device_token="qa-token-shared-with-real")
    sender_uid, sender_headers = _regular(api, token="qa-real-sender")
    target_uid, target_headers = _regular(api, token="qa-token-shared-with-real")
    response = api.client.post("/api/family_group/create", headers=sender_headers,
                               json={"user_id": sender_uid, "group_name": "일반 가족", "nickname": "일반 A"})
    assert response.status_code == 201, response.text
    assert api.client.post("/api/family_group/join", headers=target_headers,
                           json={"user_id": target_uid, "join_code": response.json()["join_code"], "nickname": "일반 B"}).status_code == 200
    before_test = dict(_row(api, alpha["user"]["user_id"]))
    response = api.client.post("/api/notifications/danger", headers=sender_headers,
                               json={"from_user_id": sender_uid, "danger_type": "test"})
    assert response.status_code == 200 and response.json()["sent_count"] == 0
    assert api.pushes == []
    assert all(push["device_token"] == "qa-real-sender" for push in api.self_pushes)
    assert dict(_row(api, alpha["user"]["user_id"])) == before_test


def test_own_counts_are_separate_and_synthetic_null_tokens_send_nothing(api):
    alpha = _login(api)
    uid = alpha["user"]["user_id"]
    regular_uid, _ = _regular(api)
    for route, count in (("warning", 9), ("danger", 3)):
        response = api.client.put("/api/notifications/" + route + "-count", headers=alpha["headers"],
                                  json={"user_id": uid, route + "_count": count})
        assert response.status_code == 200, response.text
    assert (_row(api, uid)["warning_count"], _row(api, uid)["danger_count"]) == (9, 3)
    assert (_row(api, regular_uid)["warning_count"], _row(api, regular_uid)["danger_count"]) == (7, 4)
    assert api.pushes == api.self_pushes == []


def test_test_csv_is_quarantined_and_withdrawal_never_calls_kakao_or_deletes_real_data(api):
    alpha, bravo = _login(api), _login(api, CODE_B)
    uid, other_uid = alpha["user"]["user_id"], bravo["user"]["user_id"]
    real_uid, real_headers = _regular(api)
    responses = []
    for actor, headers in ((uid, alpha["headers"]), (other_uid, bravo["headers"]), (real_uid, real_headers)):
        response = api.client.post("/api/ml/labeled-csv?user_id=" + actor, headers=headers, content=CSV_BODY)
        assert response.status_code == 201, response.text
        responses.append(response.json())
    own_file = api.test_inbox / responses[0]["filename"]
    other_file = api.test_inbox / responses[1]["filename"]
    real_file = api.production / responses[2]["filename"]
    assert own_file.name.startswith("test-account-") and own_file.read_bytes() == CSV_BODY
    assert other_file.is_file() and real_file.is_file()
    assert len(list(api.production.iterdir())) == 1
    response = api.client.request("DELETE", "/api/auth/kakao/delete", headers=alpha["headers"], json={"user_id": uid})
    assert response.status_code == 200, response.text
    assert _row(api, uid) is None and not own_file.exists()
    assert other_file.read_bytes() == CSV_BODY and real_file.read_bytes() == CSV_BODY
    assert _row(api, real_uid)["is_active"] == 1 and _row(api, other_uid)["is_active"] == 1
    assert api.client.get("/api/auth/kakao/me", headers=alpha["headers"]).status_code == 401
    api.unlink.assert_not_called()
    # Recreating a withdrawn primary must not hijack an existing fixture's family.
    recreated = _login(api)
    assert recreated["user"]["user_id"] != uid and recreated["user"]["group_id"] is None
    assert api.client.get("/api/auth/kakao/me", headers=alpha["headers"]).status_code == 401


@pytest.mark.parametrize("relative", ["same", "inside", "contains"])
def test_bad_quarantine_path_fails_closed_without_production_upload(api, monkeypatch, relative):
    alpha = _login(api)
    target = {"same": api.production, "inside": api.production / "test", "contains": api.production.parent}[relative]
    monkeypatch.setenv("TEST_ACCOUNT_CSV_DIR", str(target))
    response = api.client.post("/api/ml/labeled-csv?user_id=" + alpha["user"]["user_id"],
                               headers=alpha["headers"], content=CSV_BODY)
    assert response.status_code == 500
    assert list(api.production.iterdir()) == []


def test_training_inbox_importer_skips_copied_test_account_csv(tmp_path, monkeypatch):
    # The worker's import is lightweight; do not start its CLI/training pipeline.
    import importlib.util
    import os
    import time
    source = Path(__file__).resolve().parents[1] / "ml" / "phishing-classifier" / "auto_train.py"
    monkeypatch.syspath_prepend(str(source.parent))
    spec = importlib.util.spec_from_file_location("isolated_auth_qa_train_importer", source)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    synthetic = tmp_path / "test-account-alpha_20261001T120000_000.csv"
    regular = tmp_path / "regular_20261001T120000_000.csv"
    synthetic.write_bytes(CSV_BODY)
    regular.write_bytes(CSV_BODY.replace("테스트 계정 합성 문장".encode(), "실제 사용자 문장".encode()))
    for file in (synthetic, regular):
        os.utime(file, (time.time() - 20, time.time() - 20))
    feedback = worker.read_feedback(tmp_path)
    assert feedback == [{"text": "실제 사용자 문장", "label": "위험", "origin": "user_confirmed"}]
