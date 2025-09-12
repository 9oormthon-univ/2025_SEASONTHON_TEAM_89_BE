from fastapi import APIRouter, HTTPException, status
from app.schemas.family_group import (
    FamilyGroupCreateRequest,
    FamilyGroupCreateResponse,
    FamilyGroupJoinRequest,
    FamilyGroupJoinResponse,
    FamilyGroupInfoResponse,
    FamilyGroupCompleteResponse,
    FamilyGroupKickMemberRequest,
    FamilyGroupKickMemberResponse,
    ErrorResponse
)
from app.services.family_group_service import family_group_service

router = APIRouter()

@router.post(
    "/create",
    response_model=FamilyGroupCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="가족 그룹 생성 (대기 상태)",
    description="새로운 가족 그룹을 대기 상태로 생성 및 10자리 참여 코드를 발급함 5분 안에 타 사용자는 해당 코드로 참여해야 함."
)
async def create_family_group(request: FamilyGroupCreateRequest):
    """
    가족 그룹 생성 API (대기 상태)
    
    삭제 : # - group_name: 그룹 이름 (최대 8자)
    - user_id: 그룹을 생성하는 사용자 ID
    - user_name: 그룹을 생성하는 사용자 이름
    
    Returns:
    - 대기 상태인 그룹 정보와 10자리 참여 코드
    - 5분 안에 완성 버튼을 눌러야 정식 그룹이 됨
    """
    try:
        result = family_group_service.create_family_group(request)
        return result
    except ValueError as e:
        error_code = str(e)
        if error_code == "USER_ALREADY_IN_GROUP":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="이미 가족 그룹에 속해있음"
            )
        elif error_code == "ALREADY_CREATING_GROUP":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="이미 생성 중인 그룹이 있음"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="그룹 생성 실패"
        )

@router.post(
    "/join",
    response_model=FamilyGroupJoinResponse,
    status_code=status.HTTP_200_OK,
    summary="가족 그룹 참여",
    description="10자리 참여 코드를 사용하여 가족 그룹에 참여"
)
async def join_family_group(request: FamilyGroupJoinRequest):
    """
    가족 그룹 참여 API
    
    - join_code: 10자리 참여 코드
    - user_id: 참여하는 사용자 ID  
    - user_name: 참여하는 사용자 이름
    
    Returns:
    - 참여한 그룹 정보 (그룹명, 그룹장 이름 포함)
    """
    try:
        result = family_group_service.join_family_group(request)
        return result
    except ValueError as e:
        error_code = str(e)
        if error_code == "USER_ALREADY_IN_GROUP":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="이미 가족 그룹에 속해있음"
            )
        elif error_code == "INVALID_JOIN_CODE":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="유효하지 않은 참여 코드"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="그룹 참여 실패"
        )

@router.post(
    "/complete",
    response_model=FamilyGroupCompleteResponse,
    status_code=status.HTTP_200_OK,
    summary="그룹 생성 완료",
    description="대기 중인 그룹을 정식 그룹으로 완성함 (최초 생성자만 가능)"
)
async def complete_group_creation(user_id: str):
    """
    그룹 생성 완료 API
    
    - user_id: 그룹 생성자의 사용자 ID
    
    Returns:
    - 완성된 그룹 정보
    """
    try:
        result = await family_group_service.complete_group_creation(user_id)
        return FamilyGroupCompleteResponse(**result)
    except ValueError as e:
        error_code = str(e)
        if error_code == "NO_PENDING_GROUP":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="대기 중인 그룹이 없습니다"
            )
        elif error_code == "NOT_GROUP_CREATOR":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="그룹 생성자만 완성할 수 있습니다"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="그룹 완성 중 오류가 발생했습니다"
        )


@router.post(
    "/kick",
    response_model=FamilyGroupKickMemberResponse,
    status_code=status.HTTP_200_OK,
    summary="대기 그룹에서 멤버 추방",
    description="그룹장이 대기 중인 그룹에서 특정 멤버를 추방함"
)
async def kick_member_from_pending_group(request: FamilyGroupKickMemberRequest):
    """
    대기 그룹에서 멤버 추방 API
    
    - creator_id: 그룹장 ID (추방 권한이 있는 사용자)
    - target_user_id: 추방할 사용자 ID
    
    Returns:
    - 추방된 사용자 정보와 남은 멤버 수
    """
    try:
        result = family_group_service.kick_member_from_pending_group(
            request.creator_id, 
            request.target_user_id
        )
        return FamilyGroupKickMemberResponse(**result)
    except ValueError as e:
        error_code = str(e)
        if error_code == "NO_PENDING_GROUP":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="대기 중인 그룹이 없습니다"
            )
        elif error_code == "NOT_GROUP_CREATOR":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="그룹 생성자만 멤버를 추방할 수 있습니다"
            )
        elif error_code == "CANNOT_KICK_YOURSELF":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="자기 자신을 추방할 수 없습니다"
            )
        elif error_code == "USER_NOT_IN_GROUP":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 사용자가 그룹에 없습니다"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="멤버 추방 중 오류가 발생했습니다"
        )


@router.delete(
    "/leave/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="가족 그룹 탈퇴",
    description="가족 그룹에서 탈퇴 -> 그룹장이 탈퇴하면 그룹이 해체됨" 
    # TODO 이후 자동으로 승계하는 방법 고려할 수 있음
)
async def leave_family_group(user_id: str):
    """
    가족 그룹 탈퇴 API
    
    - user_id: 탈퇴하는 사용자 ID
    
    Note: 그룹장이 탈퇴하면 전체 그룹이 해체됨.
    """
    success = family_group_service.leave_family_group(user_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="가족 그룹에 속해있지 않음"
        )
    return {"message": "가족 그룹에서 탈퇴했습니다."}

@router.put(
    "/warning/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="사용자 경고 횟수 업데이트",
    description="사용자의 경고 횟수를 업데이트 (내부 시스템 호출용)"
)
async def update_warning_count(user_id: str, warning_count: int):
    """
    사용자 경고 횟수 업데이트 API (내부 시스템용)
    
    - user_id: 사용자 ID
    - warning_count: 새로운 경고 횟수
    """
    family_group_service.update_user_warning_count(user_id, warning_count)
    return {"message": f"사용자 {user_id}의 경고 횟수가 {warning_count}로 업데이트됨"}


@router.get(
    "/info/{user_id}",
    response_model=FamilyGroupInfoResponse,
    status_code=status.HTTP_200_OK,
    summary="가족 그룹 정보 조회",
    description="사용자가 속한 가족 그룹의 구성원 정보와 경고 횟수를 조회"
)
async def get_family_group_info(user_id: str):
    """
    가족 그룹 정보 조회 API
    
    - user_id: 조회하는 사용자 ID
    
    Returns:
    - 그룹 정보, 구성원 수, 각 구성원의 이름과 경고 횟수
    """
    result = family_group_service.get_family_group_info(user_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="가족 그룹에 속해있지 않음"
        )
    return result


@router.get(
    "/pending/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="대기 중인 그룹 정보 조회",
    description="사용자의 대기 중인 그룹 정보를 조회"
)
async def get_pending_group_info(user_id: str):
    """
    대기 중인 그룹 정보 조회 API
    
    - user_id: 사용자 ID
    
    Returns:
    - 대기 중인 그룹 정보 (있는 경우)
    """
    pending_info = family_group_service.get_pending_group_info(user_id)
    if pending_info:
        return {
            "success": True,
            "data": pending_info
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="대기 중인 그룹이 없습니다"
        )

@router.get(
    "/user/{user_id}/status",
    status_code=status.HTTP_200_OK,
    summary="사용자 그룹 상태 조회",
    description="사용자의 그룹 상태 (완성된 그룹 또는 대기 중인 그룹)를 조회"
)
async def get_user_group_status(user_id: str):
    """
    사용자 그룹 상태 조회 API
    
    - user_id: 사용자 ID
    
    Returns:
    - 그룹 상태 정보 (완성된 그룹 또는 대기 중인 그룹)
    """
    # 완성된 그룹 확인
    group_info = family_group_service.get_family_group_info(user_id)
    if group_info:
        return {
            "success": True,
            "status": "completed",
            "data": group_info
        }
    
    # 대기 중인 그룹 확인
    pending_info = family_group_service.get_pending_group_info(user_id)
    if pending_info:
        return {
            "success": True,
            "status": "pending",
            "data": pending_info
        }
    
    return {
        "success": False,
        "status": "none",
        "message": "그룹에 속해있지 않습니다"
    }

@router.delete(
    "/cancel/{creator_id}",
    status_code=status.HTTP_200_OK,
    summary="생성중 그룹 취소",
    description="생성 중인 그룹을 취소하고 대기 중인 모든 멤버를 추방합니다 (생성자 ID로 요청)"
)
async def cancel_group_creation(creator_id: str):
    """
    - creator_id: 그룹 생성자 ID
    
    Returns:
    - 취소된 그룹 정보
    - 추방된 멤버 목록
    - 취소 시간 정보
    """
    try:
        result = family_group_service.cancel_group_creation(creator_id)
        return result
    except ValueError as e:
        error_code = str(e)
        if error_code == "NO_PENDING_GROUP":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="생성 중인 그룹이 없습니다"
            )
        elif error_code == "NOT_GROUP_CREATOR":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="그룹 생성자만 그룹 생성을 취소할 수 있습니다"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="그룹 생성 취소 중 오류가 발생했습니다"
        )
