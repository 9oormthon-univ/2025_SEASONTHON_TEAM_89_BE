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
    DeviceTokenUpdateResponse,
    KakaoUnlinkRequest,
    KakaoAdminUnlinkRequest,
    KakaoUnlinkResponse
)
from app.services.kakao_service import kakao_service
from app.services.jwt_service import jwt_service
from app.repositories.user_repository import get_user_repository
from app import settings
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
        logger.info(f"JWT 토큰 생성: user_id={user.user_id}, is_new_user={is_new_user}")
        access_token = jwt_service.create_access_token(
            data={
                "user_id": user.user_id,  # UUID 사용
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
                user_id=user.user_id,  # UUID 사용
                kakao_id=int(user.kakao_id),
                nickname=user.nickname,
                profile_image=user.profile_image or ""
            ),
            is_new_user=is_new_user
        )
        
        logger.info(f"카카오 로그인 성공: user_id={user.user_id}, is_new_user={is_new_user}")
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
        user = user_repo.get_by_user_id(user_id)
        
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
        user = user_repo.get_by_user_id(user_id)
        
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

@router.post("/unlink", response_model=KakaoUnlinkResponse)
async def kakao_unlink(
    request: KakaoUnlinkRequest
):
    """
    카카오 앱 연동 해제
    
    동의화면을 다시 보기 위해 카카오 서버에서 앱 연동을 해제합니다.
    주의: 이 작업은 카카오 계정에서 완전히 앱 연돐을 해제합니다.
    """
    try:
        # 카카오 서버에서 앱 연동 해제
        logger.info("카카오 앱 연동 해제 요청")
        success = await kakao_service.unlink_user(request.access_token)
        
        if success:
            logger.info("카카오 앱 연동 해제 성공")
            return KakaoUnlinkResponse(
                success=True,
                message="카카오 앱 연동이 성공적으로 해제되었습니다. 다음 로그인에서 동의화면이 나타납니다."
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="카카오 앱 연동 해제에 실패했습니다"
            )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"카카오 앱 연동 해제 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"앱 연동 해제 실패: {str(e)}"
        )

@router.post("/admin-unlink", response_model=KakaoUnlinkResponse)
async def kakao_admin_unlink(
    request: KakaoAdminUnlinkRequest
):
    """
    카카오 관리자 권한으로 앱 연동 해제
    
    Admin Key를 사용하여 특정 사용자의 앱 연동을 강제로 해제합니다.
    개발/테스트 환경에서만 사용하세요.
    """
    try:
        # 카카오 서버에서 관리자 권한으로 앱 연동 해제
        logger.info(f"카카오 관리자 권한 앱 연동 해제 요청: kakao_id={request.kakao_id}")
        success = await kakao_service.admin_unlink_user(request.kakao_id)
        
        if success:
            logger.info(f"카카오 관리자 권한 앱 연동 해제 성공: kakao_id={request.kakao_id}")
            return KakaoUnlinkResponse(
                success=True,
                message=f"카카오 사용자 {request.kakao_id}의 앱 연동이 관리자 권한으로 해제되었습니다."
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="카카오 관리자 권한 앱 연동 해제에 실패했습니다"
            )
        
    except HTTPException:
        raise
    except ValueError as e:
        error_msg = str(e)
        if "KAKAO_ADMIN_KEY가 설정되지 않았습니다" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="서버 설정 오류: 카카오 관리자 키가 설정되지 않았습니다"
            )
        elif "KAKAO_ADMIN_KEY가 유효하지 않습니다" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="카카오 관리자 키가 유효하지 않습니다"
            )
        elif "카카오 사용자를 찾을 수 없습니다" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"카카오 사용자 ID {request.kakao_id}를 찾을 수 없습니다"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )
    except Exception as e:
        logger.error(f"카카오 관리자 권한 앱 연동 해제 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"관리자 권한 앱 연동 해제 실패: {str(e)}"
        )