from fastapi import APIRouter, HTTPException, Depends, status, Request
from sqlalchemy.orm import Session
from datetime import datetime
import logging

from app.core.database import get_db
from app.api.dependencies import enforce_actor, require_current_user
from app.schemas.kakao import DeviceTokenRegisterRequest, DeviceTokenUpdateResponse
from app.repositories.user_repository import get_user_repository
from app.services.test_account_service import (
    attempt_limiter, configured_code_hashes, read_credentials, match_slot, login_test_account,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/test-account", summary="서버 검증 테스트 계정 로그인", openapi_extra={
    "requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "required": ["code"], "additionalProperties": False,
        "properties": {"code": {"type": "string", "minLength": 22, "maxLength": 128, "writeOnly": True},
                       "device_token": {"type": "string", "maxLength": 255, "writeOnly": True}},
    }}}},
})
async def test_account_login(request: Request, db: Session = Depends(get_db)):
    config = configured_code_hashes()
    if config is None:
        raise HTTPException(503, "테스트 계정 로그인이 비활성화되어 있습니다.")
    attempt_limiter.check(request.client.host if request.client else "unknown")
    code, device_token = await read_credentials(request)
    slot = match_slot(code, config)
    try:
        return login_test_account(db, slot, device_token)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        # Database exceptions may include parameters; never log the request or credential.
        logger.error("테스트 계정 로그인 처리 실패")
        raise HTTPException(503, "테스트 계정을 사용할 수 없습니다.") from None


@router.post(
    "/device-token",
    response_model=DeviceTokenUpdateResponse,
    summary="디바이스 토큰 등록/갱신 (user_id 기반)",
    description="클라 onNewToken에서 호출 — FCM/APNs 토큰이 갱신되면 서버에 다시 등록한다. "
                "(최초 토큰은 카카오 로그인 시 device_token으로 함께 전송됨)",
)
async def register_device_token(
    request: DeviceTokenRegisterRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_current_user),
):
    """
    디바이스 토큰 등록/갱신 API

    - user_id: 사용자 ID
    - device_token: 새 FCM/APNs 디바이스 토큰

    family_group API와 동일하게 user_id 기반(세션의 user_id 사용).
    """
    enforce_actor(current_user, request.user_id)

    user_repo = get_user_repository(db)
    user = user_repo.get_by_user_id(request.user_id)

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없음",
        )

    try:
        user.device_token = request.device_token
        user.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(user)
    except Exception as e:
        db.rollback()
        logger.error(f"디바이스 토큰 갱신 실패: user_id={request.user_id}, error={str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="디바이스 토큰 갱신 실패",
        )

    logger.info(f"디바이스 토큰 갱신(POST /auth/device-token): user_id={request.user_id}")
    return DeviceTokenUpdateResponse(
        success=True,
        message="디바이스 토큰이 갱신되었습니다",
    )
