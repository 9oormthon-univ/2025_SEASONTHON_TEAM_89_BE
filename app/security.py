import secrets
from collections.abc import Mapping
from numbers import Real


_LOCAL_ENVIRONMENTS = frozenset(
    {"development", "dev", "test", "testing", "local"}
)


def actor_ids_match(authenticated_user_id: object, claimed_user_id: object) -> bool:
    """Return whether a request's claimed actor is the authenticated user."""
    if not isinstance(authenticated_user_id, str) or not authenticated_user_id:
        return False
    if not isinstance(claimed_user_id, str) or not claimed_user_id:
        return False
    return secrets.compare_digest(authenticated_user_id, claimed_user_id)


def validate_jwt_secret(
    environment: str,
    secret: str,
    insecure_fallback: str,
) -> None:
    """Fail closed when a deployed environment uses a missing or weak JWT key."""
    normalized_environment = (
        environment.strip().lower() if isinstance(environment, str) else ""
    )
    if normalized_environment in _LOCAL_ENVIRONMENTS:
        return
    if (
        not isinstance(secret, str)
        or len(secret.strip().encode("utf-8")) < 32
        or not isinstance(insecure_fallback, str)
        or secrets.compare_digest(secret, insecure_fallback)
    ):
        raise RuntimeError(
            "JWT_SECRET_KEY must be explicitly configured with at least 32 bytes "
            "outside local/test environments"
        )


def access_token_claims_are_valid(
    payload: object,
    now_timestamp: float,
    max_future_iat_seconds: int = 60,
) -> bool:
    """Validate the application-specific claims on a decoded access JWT."""
    if not isinstance(payload, Mapping):
        return False
    if payload.get("type") != "access":
        return False

    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return False

    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, Real)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, Real)
    ):
        return False

    return (
        issued_at <= now_timestamp + max_future_iat_seconds
        and expires_at > now_timestamp
        and expires_at > issued_at
    )
