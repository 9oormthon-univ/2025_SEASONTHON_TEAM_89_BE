import ast
import importlib.util
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_file_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


security = _load_file_module("security_for_test", "app/security.py")
ml_storage = _load_file_module(
    "ml_data_storage_for_test", "app/services/ml_data_storage.py"
)
legal = _load_file_module("legal_for_test", "app/api/endpoints/legal.py")


def _secured_functions(relative_path: str):
    tree = ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    secured = set()
    actor_checked = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + list(
            node.args.defaults
        )
        for default in defaults:
            if (
                isinstance(default, ast.Call)
                and isinstance(default.func, ast.Name)
                and default.func.id == "Depends"
                and default.args
                and isinstance(default.args[0], ast.Name)
                and default.args[0].id == "require_current_user"
            ):
                secured.add(node.name)

        if any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "enforce_actor"
            for child in ast.walk(node)
        ):
            actor_checked.add(node.name)

    return secured, actor_checked


def _route_functions(relative_path: str):
    tree = ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    routes = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            function = decorator.func
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "router"
                and function.attr in {"get", "post", "put", "patch", "delete"}
            ):
                routes.add(node.name)

    return routes


def _function_source(relative_path: str, function_name: str) -> str:
    source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ):
            lines = source.splitlines()
            assert node.end_lineno is not None
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"function not found: {function_name}")


def test_actor_ids_require_a_nonempty_exact_match():
    assert security.actor_ids_match("user-1", "user-1")
    assert not security.actor_ids_match("user-1", "user-2")
    assert not security.actor_ids_match("", "")
    assert not security.actor_ids_match("user-1", None)


def test_deployed_environment_rejects_missing_weak_or_fallback_jwt_key():
    fallback = "known-insecure-fallback"

    for rejected in ("", " " * 32, "too-short", fallback):
        try:
            security.validate_jwt_secret("production", rejected, fallback)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"production accepted an insecure JWT key: {rejected!r}")

    security.validate_jwt_secret("production", "x" * 32, fallback)
    security.validate_jwt_secret("test", fallback, fallback)


def test_access_tokens_require_user_type_issued_at_and_expiry_claims():
    now = 1_000.0
    valid = {
        "user_id": "user-1",
        "type": "access",
        "iat": now - 10,
        "exp": now + 10,
    }
    assert security.access_token_claims_are_valid(valid, now)

    for changes in (
        {"user_id": ""},
        {"user_id": None},
        {"type": "refresh"},
        {"iat": None},
        {"iat": now + 61},
        {"exp": None},
        {"exp": now},
        {"exp": now - 1},
        {"iat": now + 5, "exp": now + 4},
    ):
        candidate = {**valid, **changes}
        assert not security.access_token_claims_are_valid(candidate, now)


def test_jwt_service_uses_utc_aware_creation_and_epoch_verification_time():
    source = (PROJECT_ROOT / "app/services/jwt_service.py").read_text(
        encoding="utf-8"
    )
    assert "datetime.now(timezone.utc)" in source
    assert "time.time()" in source
    assert "datetime.utcnow().timestamp()" not in source


def test_user_csv_cleanup_is_exact_and_stays_inside_inbox(tmp_path):
    first_name, first_path = ml_storage.save_user_labeled_csv(
        "user-a",
        b"first",
        tmp_path,
        datetime(2026, 1, 2, 3, 4, 5, 123000),
    )
    second_name, second_path = ml_storage.save_user_labeled_csv(
        "user-a",
        b"second",
        tmp_path,
        datetime(2026, 1, 2, 3, 4, 6, 123000),
    )
    _, similarly_prefixed_path = ml_storage.save_user_labeled_csv(
        "user-a-other",
        b"other",
        tmp_path,
        datetime(2026, 1, 2, 3, 4, 7, 123000),
    )
    unrelated = tmp_path / "user-a_notes.csv"
    unrelated.write_bytes(b"keep")

    assert first_path.parent == tmp_path.resolve()
    assert first_name.startswith("user-a_")
    assert second_name.startswith("user-a_")
    assert ml_storage.delete_user_labeled_csv_files("user-a", tmp_path) == 2
    assert not first_path.exists()
    assert not second_path.exists()
    assert similarly_prefixed_path.exists()
    assert unrelated.exists()


def test_csv_cleanup_skips_matching_symlink(tmp_path):
    outside = tmp_path.parent / "outside-user-upload.csv"
    outside.write_bytes(b"keep")
    link = tmp_path / "user-a_20260102T030405_123.csv"
    link.symlink_to(outside)

    assert ml_storage.delete_user_labeled_csv_files("user-a", tmp_path) == 0
    assert link.is_symlink()
    assert outside.read_bytes() == b"keep"
    link.unlink()
    outside.unlink()


def test_all_user_routes_require_bearer_and_actor_routes_check_claim():
    expected = {
        "app/api/endpoints/auth.py": {
            "secured": {"register_device_token"},
            "actor": {"register_device_token"},
        },
        "app/api/endpoints/family_group.py": {
            "secured": {
                "create_family_group",
                "verify_group_code",
                "join_family_group",
                "kick_member_from_group",
                "leave_family_group",
                "get_family_group_info",
            },
            "actor": {
                "create_family_group",
                "join_family_group",
                "kick_member_from_group",
                "leave_family_group",
                "get_family_group_info",
            },
        },
        "app/api/endpoints/kakao_login.py": {
            "secured": {"get_current_user", "delete_user", "update_device_token"},
            "actor": {"delete_user"},
        },
        "app/api/endpoints/ml_data.py": {
            "secured": {"upload_labeled_csv"},
            "actor": {"upload_labeled_csv"},
        },
        "app/api/endpoints/notifications.py": {
            "secured": {
                "update_notification_setting",
                "send_danger_notification",
                "get_notification_settings",
                "update_danger_count_with_notification",
                "update_warning_count_with_notification",
            },
            "actor": {
                "update_notification_setting",
                "send_danger_notification",
                "get_notification_settings",
                "update_danger_count_with_notification",
                "update_warning_count_with_notification",
            },
        },
    }

    for path, expected_functions in expected.items():
        secured, actor_checked = _secured_functions(path)
        assert expected_functions["secured"] <= secured
        assert expected_functions["actor"] <= actor_checked


def test_no_unexpected_public_user_routes_are_registered():
    public_routes = {
        "app/api/endpoints/kakao_login.py": {"kakao_login_with_token"},
    }
    endpoint_files = (
        "app/api/endpoints/auth.py",
        "app/api/endpoints/family_group.py",
        "app/api/endpoints/kakao_login.py",
        "app/api/endpoints/ml_data.py",
        "app/api/endpoints/notifications.py",
    )

    for path in endpoint_files:
        routes = _route_functions(path)
        secured, _ = _secured_functions(path)
        assert routes - public_routes.get(path, set()) == routes & secured


def test_account_deletion_finishes_local_deletion_before_external_unlink():
    source = _function_source("app/api/endpoints/kakao_login.py", "delete_user")
    csv_cleanup = source.index("delete_user_labeled_csv_files(request.user_id)")
    relationship_cleanup = source.index("family_group_service.leave_family_group(")
    database_delete = source.index(
        "user_repo.delete_user(request.user_id, commit=False)"
    )
    database_commit = source.index("db.commit()")
    external_unlink = source.index("kakao_service.admin_unlink_user(int(kakao_id))")

    assert "db=db" in source
    assert "commit=False" in source
    assert (
        csv_cleanup
        < relationship_cleanup
        < database_delete
        < database_commit
        < external_unlink
    )
    assert "except Exception as unlink_error" in source


def test_group_creator_relationships_are_deleted_before_group_ids_are_cleared():
    source = _function_source(
        "app/services/family_group_service.py", "leave_family_group"
    )
    relationship_cleanup = source.index(
        "SELECT user_id FROM group_members WHERE group_id = :group_id"
    )
    group_id_clear = source.index(
        "UPDATE users SET group_id = NULL WHERE group_id = :group_id"
    )
    assert relationship_cleanup < group_id_clear


def test_user_relationship_cleanup_includes_notification_delivery_logs():
    source = _function_source(
        "app/services/family_group_service.py", "_delete_user_relationships"
    )
    assert "DELETE FROM notification_logs" in source
    assert "from_user_id = :user_id OR to_user_id = :user_id" in source


def test_legal_pages_are_public_html():
    application = FastAPI()
    application.include_router(legal.router)
    client = TestClient(application)

    expected_titles = {
        "/privacy-policy.html": "위허메 개인정보 처리방침",
        "/account-deletion.html": "위허메 계정 삭제",
    }
    for path, title in expected_titles.items():
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert title in response.text


def test_legal_router_is_mounted_at_the_public_root():
    main = (PROJECT_ROOT / "app/__main__.py").read_text(encoding="utf-8")
    assert "app.include_router(legal.router)" in main
    assert 'app.include_router(routers.router, prefix="/api")' in main


def test_internal_and_test_routes_are_not_registered():
    routers = (PROJECT_ROOT / "app/api/routers.py").read_text(encoding="utf-8")
    family = (PROJECT_ROOT / "app/api/endpoints/family_group.py").read_text(
        encoding="utf-8"
    )
    kakao = (PROJECT_ROOT / "app/api/endpoints/kakao_login.py").read_text(
        encoding="utf-8"
    )
    notifications = (
        PROJECT_ROOT / "app/api/endpoints/notifications.py"
    ).read_text(encoding="utf-8")

    assert "check_fraud_ws" not in routers
    assert '"/ws/fraud"' not in routers
    assert '"/warning/{user_id}"' not in family
    assert '"/admin-unlink"' not in kakao
    assert '"/unlink"' not in kakao
    assert '"/test"' not in notifications
    assert '"/auto-danger"' not in notifications
    assert '"/auto-warning"' not in notifications
