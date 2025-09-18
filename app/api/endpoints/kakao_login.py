from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from datetime import datetime
from app.core.database import get_db
from app.schemas.kakao import (
    KakaoTokenLoginRequest,
    LoginResponse, 
    UserResponse,
    KakaoUserProfile,
    DeviceTokenUpdateRequest,
    DeviceTokenUpdateResponse
)
from app.services.kakao_service import kakao_service
from app.services.jwt_service import jwt_service
from app.repositories.user_repository import get_user_repository
from app.config import settings
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/token", response_model=LoginResponse)
async def kakao_login_with_token(
    request: KakaoTokenLoginRequest,
    db: Session = Depends(get_db)
):
    """
    카카오 SDK 토큰을 이용한 간단 로그인
    
    iOS/Android SDK에서 이미 받은 카카오 액세스 토큰으로 직접 로그인 -> access_token 사용
    """
    try:
        # 1. 카카오 사용자 정보 조회
        logger.info("카카오 사용자 정보 요청")
        user_profile = await kakao_service.get_user_info(request.access_token)
        
        # 2. 사용자 조회 또는 생성
        logger.info(f"사용자 정보 : kakao_id={user_profile.kakao_id}")
        
        user_repo = get_user_repository(db)
        user, is_new_user = user_repo.get_or_create_user(user_profile, request.device_token)
        
        # 3. JWT 토큰 생성
        logger.info(f"JWT 토큰 생성: user_id={user.id}, is_new_user={is_new_user}")
        access_token = jwt_service.create_access_token(
            data={
                "user_id": user.id,
                "kakao_id": user.kakao_id,
                "nickname": user.nickname
            }
        )
        
        # 4. JSON 응답 반환
        response = LoginResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=KakaoUserProfile(
                kakao_id=int(user.kakao_id),
                nickname=user.nickname,
                profile_image=user.profile_image or ""
            ),
            is_new_user=is_new_user
        )
        
        logger.info(f"카카오 로그인 성공: user_id={user.id}, is_new_user={is_new_user}")
        return response
        
    except ValueError as e:
        logger.warning(f"카카오 로그인 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"카카오 로그인 처리 중 서버 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"로그인 처리 실패: {str(e)}"
        )

@router.get("/me", response_model=UserResponse)
async def get_current_user(
    token: str,
    db: Session = Depends(get_db)
):
    """
    현재 로그인한 사용자 정보 조회
    
    JWT 토큰을 사용하여 현재 사용자의 정보를 반환
    """
    try:
        # JWT 토큰 검증
        payload = jwt_service.verify_token(token)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 토큰",
                headers={"WWW-Authenticate": "Bearer"}
            )
        
        # 사용자 조회
        user_id = payload.get("user_id")
        user_repo = get_user_repository(db)
        user = user_repo.get_by_id(user_id)
        
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없음"
            )
        
        return UserResponse.from_orm(user)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 정보 조회 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 정보 조회 실패"
        )

@router.patch("/device-token", response_model=DeviceTokenUpdateResponse)
async def update_device_token(
    request: DeviceTokenUpdateRequest,
    token: str,
    db: Session = Depends(get_db)
):
    """
    디바이스 토큰 업데이트
    
    APNs 푸시 알림을 받기 위한 디바이스 토큰을 업데이트
    """
    try:
        # JWT 토큰 검증
        payload = jwt_service.verify_token(token)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 토큰",
                headers={"WWW-Authenticate": "Bearer"}
            )
        
        # 사용자 조회
        user_id = payload.get("user_id")
        user_repo = get_user_repository(db)
        user = user_repo.get_by_id(user_id)
        
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없음"
            )
        
        # 디바이스 토큰 업데이트
        user.device_token = request.device_token
        user.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(user)
        
        logger.info(f"디바이스 토큰 업데이트 성공: user_id={user_id}")
        
        return DeviceTokenUpdateResponse(
            success=True,
            message="디바이스 토큰이 성공적으로 업데이트되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"디바이스 토큰 업데이트 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="디바이스 토큰 업데이트 실패"
        )