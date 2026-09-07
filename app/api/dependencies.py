from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.user import User
from app.repositories.user_repository import get_user_repository
from app.security import actor_ids_match
from app.services.jwt_service import jwt_service


bearer_auth = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="유효한 Bearer 인증이 필요합니다",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_auth),
    db: Session = Depends(get_db),
) -> User:
    """Authenticate an access JWT and return its active database user."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    payload = jwt_service.verify_token(credentials.credentials)
    user_id = payload.get("user_id") if payload else None
    if not isinstance(user_id, str) or not user_id:
        raise _unauthorized()

    user = get_user_repository(db).get_by_user_id(user_id)
    if user is None or not user.is_active:
        raise _unauthorized()
    return user


def enforce_actor(current_user: User, claimed_user_id: str) -> None:
    """Reject attempts to act as a user other than the JWT subject."""
    if not actor_ids_match(str(current_user.user_id), claimed_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="인증된 사용자와 요청 사용자 ID가 일치하지 않습니다",
        )
