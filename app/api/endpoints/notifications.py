from fastapi import APIRouter, HTTPException, status
from typing import List
from app.schemas.family_group import (
    NotificationSettingRequest,
    NotificationSettingResponse,
    DangerNotificationRequest,
    DangerNotificationResponse,
    AutoDangerNotificationRequest,
    UpdateDangerCountRequest
)
from app.services.notification_service import notification_service

router = APIRouter()

@router.post(
    "/setting",
    response_model=NotificationSettingResponse,
    status_code=status.HTTP_200_OK,
    summary="알림 설정 변경",
    description="특정 구성원으로부터의 알림을 활성화/비활성화"
)
async def update_notification_setting(request: NotificationSettingRequest):
    """
    알림 설정 변경 API
    
    - user_id: 설정을 변경하는 사용자 ID
    - target_user_id: 알림 대상 사용자 ID
    - enabled: 알림 활성화 여부 (True/False)
    
    Returns:
    - 설정 변경 결과
    """
    try:
        result = notification_service.update_notification_setting(request)
        return result
    except ValueError as e:
        error_code = str(e)
        if error_code == "USERS_NOT_IN_SAME_GROUP":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="같은 그룹에 속한 사용자들만 알림 설정이 가능합니다"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="알림 설정 변경 실패"
        )

@router.post(
    "/danger",
    response_model=DangerNotificationResponse,
    status_code=status.HTTP_200_OK,
    summary="위험 알림 전송",
    description="그룹 내 모든 구성원에게 위험 상황 알림 전송"
)
async def send_danger_notification(request: DangerNotificationRequest):
    """
    위험 알림 전송 API
    
        - from_user_id: 위험 상황을 보고하는 사용자 ID
    - danger_type: 위험 유형
    - message: 추가 메시지 (선택사항)
    
    Returns:
    - 알림 전송 결과 (전송 성공 수, 시간 등)
    """
    try:
        result = await notification_service.send_danger_notification(request)
        return result
    except ValueError as e:
        error_code = str(e)
        if error_code == "USER_NOT_IN_GROUP":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="그룹에 속한 사용자만 위험 알림을 전송할 수 있습니다"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="위험 알림 전송 실패"
        )

@router.get(
    "/settings/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="알림 설정 조회",
    description="사용자의 모든 알림 설정 조회"
)
async def get_notification_settings(user_id: str):
    """
    알림 설정 조회 API
    
    - user_id: 조회하는 사용자 ID
    
    Returns:
    - 그룹 내 모든 구성원에 대한 알림 설정 목록
    """
    try:
        settings = notification_service.get_notification_settings(user_id)
        return {
            "success": True,
            "data": settings
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="알림 설정 조회 실패"
        )

@router.post(
    "/test",
    status_code=status.HTTP_200_OK,
    summary="알림 테스트",
    description="특정 디바이스 토큰으로 테스트 알림 전송"
)
async def test_notification(device_token: str, message: str = "테스트 알림"):
    """
    알림 테스트 API
    
    - device_token: 테스트할 디바이스 토큰
    - message: 전송할 메시지
    
    Returns:
    - 전송 결과
    """
    try:
        success = await notification_service._send_apns_notification(
            device_token=device_token,
            sender_nickname="시스템",
            danger_type="test",
            message=message
        )
        return {
            "success": success,
            "message": "테스트 알림이 전송되었습니다" if success else "알림 전송에 실패했습니다"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"테스트 알림 전송 실패: {str(e)}"
        )

@router.post(
    "/auto-danger",
    response_model=DangerNotificationResponse,
    status_code=status.HTTP_200_OK,
    summary="자동 위험 알림 전송",
    description="위험 카운트 증가 시 자동으로 그룹 내 모든 구성원에게 알림 전송"
)
async def send_auto_danger_notification(request: AutoDangerNotificationRequest):
    """
    자동 위험 알림 전송 API
    
    - user_id: 위험 카운트가 증가한 사용자 ID
    - danger_count: 새로운 위험 카운트
    - trigger_reason: 알림 발생 원인 (fraud_detection, manual_report 등)
    
    Returns:
    - 자동 알림 전송 결과
    """
    try:
        result = await notification_service.send_auto_danger_notification(request)
        return result
    except ValueError as e:
        error_code = str(e)
        if error_code == "USER_NOT_IN_GROUP":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="그룹에 속한 사용자만 자동 알림을 전송할 수 있습니다"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="자동 위험 알림 전송 실패"
        )

@router.put(
    "/danger-count",
    status_code=status.HTTP_200_OK,
    summary="위험 카운트 업데이트 및 자동 알림",
    description="사용자의 위험 카운트를 업데이트하고 증가 시 자동으로 그룹에 알림 전송"
)
async def update_danger_count_with_notification(request: UpdateDangerCountRequest):
    """
    위험 카운트 업데이트 및 자동 알림 API
    
    - user_id: 업데이트할 사용자 ID
    - danger_count: 새로운 위험 카운트
    - trigger_reason: 업데이트 원인
    
    Returns:
    - 업데이트 결과 및 알림 전송 여부
    """
    try:
        success = notification_service.update_danger_count_with_notification(request)
        if success:
            return {
                "success": True,
                "message": f"사용자 {request.user_id}의 위험 카운트가 {request.danger_count}로 업데이트되었습니다.",
                "auto_notification_sent": request.danger_count > 0
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="위험 카운트 업데이트 실패"
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"위험 카운트 업데이트 실패: {str(e)}"
        )