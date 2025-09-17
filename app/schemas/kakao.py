from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

# 카카오 로그인 요청/응답 스키마
class KakaoLoginRequest(BaseModel):
    """카카오 로그인 요청"""
    authorization_code: str = Field(..., description="카카오에서 받은 인가 코드")
    redirect_uri: str = Field(..., description="리다이렉트 URI")

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

class UserResponse(BaseModel):
    """사용자 정보 응답"""
    id: int = Field(..., description="내부 사용자 ID")
    kakao_id: str = Field(..., description="카카오 사용자 ID") 
    nickname: str = Field(..., description="사용자 닉네임")
    profile_image: Optional[str] = Field(None, description="프로필 이미지 URL")
    is_active: bool = Field(..., description="활성 상태")
    created_at: datetime = Field(..., description="계정 생성일")
    last_login_at: Optional[datetime] = Field(None, description="마지막 로그인 시간")
    
    class Config:
        from_attributes = True

class KakaoLoginUrlResponse(BaseModel):
    """카카오 로그인 URL 응답"""
    login_url: str = Field(..., description="카카오 로그인 URL")
    state: Optional[str] = Field(None, description="CSRF 방지용 state 값")
    redirect_uri: Optional[str] = Field(None, description="리다이렉트 URI (모바일용)")
    client_id: Optional[str] = Field(None, description="카카오 클라이언트 ID")

# 내부 사용 스키마
class KakaoTokenResponse(BaseModel):
    """카카오 토큰 응답 (내부 사용)"""
    access_token: str
    token_type: str
    refresh_token: Optional[str] = None
    expires_in: int
    scope: Optional[str] = None

class KakaoUserInfoResponse(BaseModel):
    """카카오 사용자 정보 응답 (내부 사용)"""
    id: int
    connected_at: str
    kakao_account: dict