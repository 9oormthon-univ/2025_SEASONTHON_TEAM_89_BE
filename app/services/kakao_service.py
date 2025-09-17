import httpx
from typing import Optional
from app.config import settings
from app.schemas.kakao import KakaoTokenResponse, KakaoUserInfoResponse, KakaoUserProfile
import logging

logger = logging.getLogger(__name__)

class KakaoService:
    """카카오 API 서비스

    카카오 동의항목:
    - 닉네임 (필수)
    - 프로필 사진 (선택)
    """
    
    KAKAO_AUTH_HOST = "https://kauth.kakao.com"
    KAKAO_API_HOST = "https://kapi.kakao.com"
    
    def __init__(self):
        self.client_id = settings.kakao_client_url  # 기존 필드명 사용
    
    def get_login_url(self, redirect_uri: str, state: Optional[str] = None) -> str:
        """카카오 로그인 URL 생성"""
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
        
        if state:
            params["state"] = state
            
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{self.KAKAO_AUTH_HOST}/oauth/authorize?{query_string}"
    
    async def get_access_token(self, authorization_code: str, redirect_uri: str) -> KakaoTokenResponse:
        """인가 코드로 액세스 토큰 요청"""
        url = f"{self.KAKAO_AUTH_HOST}/oauth/token"
        
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "code": authorization_code,
        }
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, data=data, headers=headers)
                response.raise_for_status()
                
                token_data = response.json()
                logger.info(f"카카오 토큰 획득 성공: {token_data.get('access_token', 'N/A')[:10]}...")
                
                return KakaoTokenResponse(**token_data)
                
            except httpx.HTTPStatusError as e:
                logger.error(f"카카오 토큰 요청 실패: {e.response.status_code} - {e.response.text}")
                raise ValueError(f"카카오 토큰 요청 실패: {e.response.status_code}")
            except Exception as e:
                logger.error(f"카카오 토큰 요청 중 오류: {str(e)}")
                raise ValueError(f"카카오 토큰 요청 중 오류 발생")
    
    async def get_user_info(self, access_token: str) -> KakaoUserProfile:
        """액세스 토큰으로 사용자 정보 조회"""
        url = f"{self.KAKAO_API_HOST}/v2/user/me"
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                user_data = response.json()
                logger.info(f"카카오 사용자 정보 조회 성공: {user_data.get('id')}")
                
                # 카카오 응답에서 필요한 정보 추출
                kakao_account = user_data.get("kakao_account", {})
                profile = kakao_account.get("profile", {})
                
                return KakaoUserProfile(
                    kakao_id=user_data["id"],
                    nickname=profile.get("nickname", "Unknown"),
                    profile_image=profile.get("profile_image_url")
                )
                
            except httpx.HTTPStatusError as e:
                logger.error(f"카카오 사용자 정보 요청 실패: {e.response.status_code} - {e.response.text}")
                raise ValueError(f"카카오 사용자 정보 요청 실패: {e.response.status_code}")
            except Exception as e:
                logger.error(f"카카오 사용자 정보 요청 중 오류: {str(e)}")
                raise ValueError(f"카카오 사용자 정보 요청 중 오류 발생")
    
    async def unlink_user(self, access_token: str) -> bool:
        """사용자 회원 탈퇴"""
        url = f"{self.KAKAO_API_HOST}/v1/user/unlink"
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers)
                response.raise_for_status()
                
                result = response.json()
                logger.info(f"카카오 사용자 연결 해제 성공: {result.get('id')}")
                return True
                
            except Exception as e:
                logger.error(f"카카오 사용자 연결 해제 실패: {str(e)}")
                return False

kakao_service = KakaoService()