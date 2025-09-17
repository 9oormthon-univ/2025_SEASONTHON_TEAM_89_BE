from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.schemas.kakao import (
    KakaoLoginRequest, 
    LoginResponse, 
    KakaoLoginUrlResponse,
    UserResponse,
    KakaoUserProfile
)
from app.services.kakao_service import kakao_service
from app.services.jwt_service import jwt_service
from app.repositories.user_repository import get_user_repository
from app.config import settings
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/login", response_model=KakaoLoginUrlResponse)
async def get_kakao_login_url():
    """
    카카오 로그인 URL 반환

    Custom URL Scheme을 사용하여 앱으로 다시 돌아옴
    """
    try:
        redirect_uri = f"{settings.APP_CUSTOM_SCHEME}://kakao-callback"
        
        login_url = kakao_service.get_login_url(
            redirect_uri=redirect_uri
        )
        
        return KakaoLoginUrlResponse(
            login_url=login_url,
            state=None,
            redirect_uri=redirect_uri,
            client_id=settings.kakao_client_url
        )
        
    except Exception as e:
        logger.error(f"모바일 로그인 URL 생성 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="로그인 URL 생성에 실패했습니다"
        )

@router.post("/callback", response_model=LoginResponse)
async def kakao_login_callback(
    request: KakaoLoginRequest,
    db: Session = Depends(get_db)
):
    """
    카카오 로그인 콜백 처리
    
    모바일 앱에서 인가 코드를 받고 -> 이 API로 POST 요청을 보냄
    -> 이후 JSON 응답을 반환하여 모바일 앱에서 처리
    """
    try:
        # 1. 카카오 액세스 토큰 획득
        logger.info(f"카카오 로그인 시작 - code: {request.authorization_code[:10]}...")
        token_response = await kakao_service.get_access_token(
            request.authorization_code, 
            request.redirect_uri
        )
        
        # 2. 카카오 사용자 정보 조회
        logger.info("카카오 사용자 정보 요청 시작")
        user_profile = await kakao_service.get_user_info(token_response.access_token)
        
        # 3. 사용자 조회 또는 생성
        logger.info(f"사용자 정보 : kakao_id={user_profile.kakao_id}")
        
        user_repo = get_user_repository(db)
        user, is_new_user = user_repo.get_or_create_user(user_profile)
        
        # 4. JWT 토큰 생성
        logger.info(f"JWT 토큰 생성: user_id={user.id}, is_new_user={is_new_user}")
        access_token = jwt_service.create_access_token(
            data={
                "user_id": user.id,
                "kakao_id": user.kakao_id,
                "nickname": user.nickname
            }
        )
        
        # 5. JSON 응답 반환
        response = LoginResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=KakaoUserProfile(
                kakao_id=int(user.kakao_id),
                nickname=user.nickname
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