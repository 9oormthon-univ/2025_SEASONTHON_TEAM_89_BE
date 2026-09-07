from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from datetime import datetime
from app.core.database import get_db
from app.api.dependencies import enforce_actor, require_current_user
from app.schemas.kakao import (
    KakaoTokenLoginRequest,
    LoginResponse, 
    UserResponse,
    KakaoUserProfile,
    DeviceTokenUpdateRequest,
    DeviceTokenUpdateResponse,
    UserDeleteRequest,
)
from app.services.kakao_service import kakao_service
from app.services.jwt_service import jwt_service
from app.services.family_group_service import family_group_service
from app.services.ml_data_storage import delete_user_labeled_csv_files
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
            detail="로그인 처리 실패"
        )

@router.get("/me", response_model=UserResponse)
async def get_current_user(
    current_user=Depends(require_current_user),
):
    """
    현재 로그인한 사용자 정보 조회
    
    JWT 토큰을 사용하여 현재 사용자의 정보를 반환
    """
    try:
        return UserResponse.from_orm(current_user)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 정보 조회 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 정보 조회 실패"
        )


@router.delete("/delete", status_code=status.HTTP_200_OK)
async def delete_user(
    request: UserDeleteRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_current_user),
):
    """
    회원 탈퇴
    
    1. 가족 그룹 탈퇴 (그룹장일 경우 그룹 해체도)
    2. 카카오 앱 연동 해제
    3. 사용자 데이터 삭제
    
    Returns:
    - 성공 시 200 OK만
    - 실패 시 HTTP 에러 코드
    """
    enforce_actor(current_user, request.user_id)
    try:
        user_repo = get_user_repository(db)
        
        # 1. 사용자 조회
        user = user_repo.get_by_user_id(request.user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다"
            )
        
        logger.info(f"회원 탈퇴 시작: user_id={request.user_id}, kakao_id={user.kakao_id}")
        
        # 2. 사용자가 업로드한 ML 라벨링 원본 삭제. 실패하면 계정 DB 변경 전에 중단한다.
        deleted_csv_count = delete_user_labeled_csv_files(request.user_id)
        logger.info(
            f"회원 탈퇴 ML CSV 삭제 완료: user_id={request.user_id}, count={deleted_csv_count}"
        )

        # 3. 가족 그룹 및 알림 관계 삭제. 그룹이 없어도 고아 알림 관계를 정리한다.
        logger.info(
            f"가족 그룹 관계 삭제 시도: user_id={request.user_id}, group_id={user.group_id}"
        )
        group_leave_success = family_group_service.leave_family_group(
            request.user_id,
            db=db,
            commit=False,
        )
        if not group_leave_success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="가족 그룹 관계 삭제에 실패했습니다"
            )
        logger.info(f"가족 그룹 관계 삭제 성공: user_id={request.user_id}")

        # 4. 사용자 데이터 삭제
        kakao_id = str(user.kakao_id)
        delete_success = user_repo.delete_user(request.user_id, commit=False)
        if not delete_success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="사용자 데이터 삭제에 실패했습니다"
            )
        db.commit()

        # 5. 외부 카카오 연동 해제는 로컬 계정 삭제를 막지 않는 후속 정리로 처리한다.
        try:
            logger.info(f"카카오 앱 연동 해제 시도: kakao_id={kakao_id}")
            kakao_unlink_success = await kakao_service.admin_unlink_user(int(kakao_id))
            if kakao_unlink_success:
                logger.info(f"카카오 앱 연동 해제 성공: kakao_id={kakao_id}")
            else:
                logger.warning(f"카카오 앱 연동 해제 실패: kakao_id={kakao_id}")
        except Exception as unlink_error:
            logger.warning(
                "카카오 앱 연동 해제 후속 처리 실패: kakao_id=%s, error=%s",
                kakao_id,
                str(unlink_error),
            )

        logger.info(f"회원 탈퇴 완료: user_id={request.user_id}")
        # 200 OK만 반환
        
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"회원 탈퇴 처리 중 오류: user_id={request.user_id}, error={str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="회원 탈퇴 처리 실패"
        )

@router.patch("/device-token", response_model=DeviceTokenUpdateResponse)
async def update_device_token(
    request: DeviceTokenUpdateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_current_user),
):
    """
    디바이스 토큰 업데이트
    
    APNs 푸시 알림을 받기 위한 디바이스 토큰을 업데이트
    """
    try:
        # 디바이스 토큰 업데이트
        current_user.device_token = request.device_token
        current_user.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(current_user)
        
        logger.info(f"디바이스 토큰 업데이트 성공: user_id={current_user.user_id}")
        
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
