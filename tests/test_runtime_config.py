import os
from pathlib import Path
import subprocess
import sys
import textwrap


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_import(script: str, environment: dict[str, str]):
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _test_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "ENVIRONMENT": "test",
            "SERVER_HOST": "127.0.0.9",
            "SERVER_PORT": "8765",
            "OLLAMA_URL": "http://127.0.0.1:11434",
            "OLLAMA_MODEL": "test-model",
            "IS_PRODUCTION": "false",
        }
    )
    environment.pop("WEB_HOST", None)
    environment.pop("WEB_PORT", None)
    return environment


def test_clean_clone_import_uses_environment_backed_tracked_config():
    result = _run_import(
        """
        import app
        assert app.WEB_HOST == "127.0.0.9"
        assert app.WEB_PORT == 8765
        assert app.OLLAMA_MODEL == "test-model"
        assert app.IS_PRODUCTION is False
        """,
        _test_environment(),
    )
    assert result.returncode == 0, result.stderr


def test_existing_local_config_override_remains_authoritative():
    result = _run_import(
        """
        import sys
        import types

        config_module = types.ModuleType("app.config")
        class Development:
            WEB_HOST = "override-host"
            WEB_PORT = 4321
            OLLAMA_URL = "http://override"
            OLLAMA_MODEL = "override-model"
            AUTH_KEY_PATH = ""
            TEAM_ID = ""
            AUTH_KEY_ID = ""
            APP_BUNDLE_ID = "override.bundle"
            IS_PRODUCTION = False
            FIREBASE_CREDENTIALS_PATH = ""
            ALERT_THRESHOLD = 7
        config_module.Development = Development
        sys.modules["app.config"] = config_module

        import app
        assert app.WEB_HOST == "override-host"
        assert app.WEB_PORT == 4321
        assert app.OLLAMA_MODEL == "override-model"
        assert app.ALERT_THRESHOLD == 7
        """,
        _test_environment(),
    )
    assert result.returncode == 0, result.stderr
