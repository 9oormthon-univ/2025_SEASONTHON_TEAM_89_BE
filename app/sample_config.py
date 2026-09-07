import os


def _environment_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Non-secret runtime defaults used when no local app/config.py exists."""

    # Server. SERVER_* matches the Settings names; WEB_* remains compatible
    # with existing deployments.
    WEB_HOST = os.getenv("WEB_HOST", os.getenv("SERVER_HOST", "0.0.0.0"))
    WEB_PORT = int(os.getenv("WEB_PORT", os.getenv("SERVER_PORT", "8000")))

    # Ollama fraud-analysis worker
    OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "fraud-detector:latest")

    # Push Alarm (APNs - iOS). Empty credentials disable initialization.
    AUTH_KEY_PATH = os.getenv("AUTH_KEY_PATH", "")
    TEAM_ID = os.getenv("TEAM_ID", "")
    AUTH_KEY_ID = os.getenv("AUTH_KEY_ID", "")
    APP_BUNDLE_ID = os.getenv("APP_BUNDLE_ID", "")
    IS_PRODUCTION = _environment_bool("IS_PRODUCTION", default=False)

    # Push Alarm (FCM - Android). The credential file remains external.
    FIREBASE_CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH", "")

    # 가족 알림 빈도 제한 (위험 N번에 한 번 전송)
    ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", "3"))
