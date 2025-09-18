from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

# 카카오 로그인 요청/응답 스키마
class KakaoTokenLoginRequest(BaseModel):
    """카카오 SDK 토큰 로그인 요청"""
    access_token: str = Field(..., description="카카오 SDK에서 받은 액세스 토큰")
    device_token: Optional[str] = Field(None, description="APNs 디바이스 토큰 (푸시 알림용)")

class KakaoUserProfile(BaseModel):
    """카카오 사용자 프로필 정보"""
    kakao_id: int = Field(..., description="카카오 사용자 ID")
    nickname: str = Field(..., description="사용자 닉네임")
    profile_image: Optional[str] = Field(None, description="프로필 이미지 URL")

class LoginResponse(BaseModel):
    """로그인 응답"""
    access_token: str = Field(..., description="JWT 액세스 토큰")
    token_type: str = Field(default="bearer", description="토큰 타입")
    expires_in: int = Field(..., description="토큰 만료 시간(초)")
    user: KakaoUserProfile = Field(..., description="사용자 정보")
    is_new_user: bool = Field(..., description="신규 사용자 여부")

# 디바이스 토큰 업데이트 스키마
class DeviceTokenUpdateRequest(BaseModel):
    """디바이스 토큰 업데이트 요청"""
    device_token: str = Field(..., description="새로운 APNs 디바이스 토큰")

class DeviceTokenUpdateResponse(BaseModel):
    """디바이스 토큰 업데이트 응답"""
    success: bool = Field(..., description="업데이트 성공 여부")
    message: str = Field(..., description="결과 메시지")

class UserResponse(BaseModel):
    """사용자 정보 응답"""
    id: int = Field(..., description="내부 사용자 ID")
    kakao_id: str = Field(..., description="카카오 사용자 ID") 
    nickname: str = Field(..., description="사용자 닉네임")
    profile_image: Optional[str] = Field(None, description="프로필 이미지 URL")
    group_id: Optional[str] = Field(None, description="현재 속한 그룹 ID")
    warning_count: int = Field(default=0, description="주의 받은 횟수")
    danger_count: int = Field(default=0, description="위험 받은 횟수")
    device_token: Optional[str] = Field(None, description="APNs 디바이스 토큰")
    is_active: bool = Field(..., description="활성 상태")
    created_at: datetime = Field(..., description="계정 생성일")
    last_login_at: Optional[datetime] = Field(None, description="마지막 로그인 시간")
    
    class Config:
        from_attributes = True

# 내부 사용 스키마
class KakaoUserInfoResponse(BaseModel):
    """카카오 사용자 정보 응답 (내부 사용)"""
    id: int
    connected_at: str
    kakao_account: dict