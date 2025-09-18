from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

# 가족 그룹 생성 요청
class FamilyGroupCreateRequest(BaseModel):
    user_id: str = Field(..., description="그룹을 생성하는 사용자 ID")
    group_name: str = Field(..., min_length=1, max_length=30, description="그룹 이름")
    nickname: str = Field(..., min_length=1, max_length=20, description="그룹에서 사용할 별명")

# 가족 그룹 생성 응답
class FamilyGroupCreateResponse(BaseModel):
    group_id: str = Field(..., description="생성된 그룹 ID")
    group_name: str = Field(..., description="그룹 이름")
    join_code: str = Field(..., description="10자리 참여 코드")
    creator_id: str = Field(..., description="그룹장 ID")
    created_at: datetime = Field(..., description="생성 시간")

# 가족 그룹 참여 요청
class FamilyGroupJoinRequest(BaseModel):
    join_code: str = Field(..., min_length=10, max_length=10, description="10자리 참여 코드")
    user_id: str = Field(..., description="참여하는 사용자 ID")
    nickname: str = Field(..., min_length=1, max_length=20, description="그룹에서 사용할 별명")

# 가족 그룹 참여 응답
class FamilyGroupJoinResponse(BaseModel):
    group_id: str = Field(..., description="참여한 그룹 ID")
    joined_at: datetime = Field(..., description="참여 시간")

# 가족 구성원 정보
class FamilyMember(BaseModel):
    user_id: str = Field(..., description="사용자 ID")
    nickname: str = Field(..., description="그룹에서 사용하는 별명")
    profile_image: Optional[str] = Field(None, description="프로필 이미지 URL")
    warning_count: int = Field(default=0, description="주의 받은 횟수")
    danger_count: int = Field(default=0, description="위험 받은 횟수")
    is_creator: bool = Field(..., description="그룹장 여부")
    joined_at: datetime = Field(..., description="그룹 참여 시간")

# 가족 그룹 정보 조회 응답
class FamilyGroupInfoResponse(BaseModel):
    group_id: str = Field(..., description="그룹 ID")
    group_name: str = Field(..., description="그룹 이름")
    join_code: str = Field(..., description="참여 코드")
    creator_id: str = Field(..., description="그룹장 ID")
    member_count: int = Field(..., description="구성원 수")
    members: List[FamilyMember] = Field(..., description="구성원 목록")
    created_at: datetime = Field(..., description="그룹 생성 시간")

# 멤버 추방 요청
class FamilyGroupKickMemberRequest(BaseModel):
    creator_id: str = Field(..., description="그룹장 ID")
    target_user_id: str = Field(..., description="추방할 사용자 ID")

# 멤버 추방 응답
class FamilyGroupKickMemberResponse(BaseModel):
    success: bool = Field(..., description="추방 성공 여부")
    kicked_user_id: str = Field(..., description="추방된 사용자 ID")
    remaining_members: int = Field(..., description="남은 멤버 수")
    message: str = Field(..., description="결과 메시지")

# 에러 응답
class ErrorResponse(BaseModel):
    error: str = Field(..., description="에러 메시지")

# APNs 알림 설정 요청
class NotificationSettingRequest(BaseModel):
    user_id: str = Field(..., description="설정을 변경하는 사용자 ID")
    target_user_id: str = Field(..., description="알림 대상 사용자 ID")
    enabled: bool = Field(..., description="알림 활성화 여부")

# APNs 알림 설정 응답
class NotificationSettingResponse(BaseModel):
    success: bool = Field(..., description="설정 변경 성공 여부")
    user_id: str = Field(..., description="설정 변경한 사용자 ID")
    target_user_id: str = Field(..., description="알림 대상 사용자 ID")
    enabled: bool = Field(..., description="알림 활성화 여부")
    message: str = Field(..., description="결과 메시지")

# 위험 알림 전송 요청
class DangerNotificationRequest(BaseModel):
    from_user_id: str = Field(..., description="위험 상황을 보고하는 사용자 ID")
    danger_type: str = Field(..., description="위험 유형 (fraud, emergency, etc.)")
    message: Optional[str] = Field(None, description="추가 메시지")

# 위험 알림 전송 응답
class DangerNotificationResponse(BaseModel):
    success: bool = Field(..., description="알림 전송 성공 여부")
    sent_count: int = Field(..., description="전송된 알림 수")
    group_id: str = Field(..., description="그룹 ID")
    from_user_id: str = Field(..., description="위험을 보고한 사용자 ID")
    timestamp: datetime = Field(..., description="알림 전송 시간")
    code: Optional[str] = Field(None, description="에러 코드")

# 위험 카운트 자동 알림 요청
class AutoDangerNotificationRequest(BaseModel):
    user_id: str = Field(..., description="위험 카운트가 증가한 사용자 ID")
    danger_count: int = Field(..., description="새로운 위험 카운트")
    trigger_reason: str = Field(..., description="알림 발생 원인 (fraud_detection, manual_report 등)")

# 위험 카운트 업데이트 요청
class UpdateDangerCountRequest(BaseModel):
    user_id: str = Field(..., description="업데이트할 사용자 ID")
    danger_count: int = Field(..., description="새로운 위험 카운트")
    trigger_reason: Optional[str] = Field("manual", description="업데이트 원인")

# 주의 카운트 자동 알림 요청
class AutoWarningNotificationRequest(BaseModel):
    user_id: str = Field(..., description="주의 카운트가 증가한 사용자 ID")
    warning_count: int = Field(..., description="새로운 주의 카운트")
    trigger_reason: str = Field(..., description="알림 발생 원인 (fraud_detection, manual_report 등)")

# 주의 카운트 업데이트 요청
class UpdateWarningCountRequest(BaseModel):
    user_id: str = Field(..., description="업데이트할 사용자 ID")
    warning_count: int = Field(..., description="새로운 주의 카운트")
    trigger_reason: Optional[str] = Field("manual", description="업데이트 원인")
