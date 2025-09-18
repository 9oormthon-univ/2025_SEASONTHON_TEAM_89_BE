import httpx
from app import settings
from app.schemas.kakao import KakaoUserProfile
import logging

logger = logging.getLogger(__name__)

class KakaoService:
    """카카오 API 서비스
    
    카카오 동의항목:
    - 닉네임 (필수)
    - 프로필 사진 (선택)
    """
    
    KAKAO_API_HOST = "https://kapi.kakao.com"
    
    def __init__(self):
        # 일단 냅둠
        self.client_id = settings.kakao_client_url

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

    async def admin_unlink_user(self, kakao_id: int) -> bool:
        """Admin Key를 사용한 강제 사용자 연결 해제"""
        # Admin Key 유효성 검사
        admin_key = getattr(settings, 'KAKAO_ADMIN_KEY', '')
        if not admin_key or admin_key.strip() == '':
            logger.error("KAKAO_ADMIN_KEY가 설정되지 않았습니다. 환경변수를 확인하세요.")
            raise ValueError("KAKAO_ADMIN_KEY가 설정되지 않았습니다")
        
        url = f"{self.KAKAO_API_HOST}/v1/user/unlink"
        
        headers = {
            "Authorization": f"KakaoAK {admin_key}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        data = {
            "target_id_type": "user_id",
            "target_id": kakao_id
        }
        
        async with httpx.AsyncClient() as client:
            try:
                logger.info(f"카카오 Admin API 호출: kakao_id={kakao_id}, admin_key={'*' * len(admin_key)}")
                response = await client.post(url, headers=headers, data=data)
                response.raise_for_status()
                
                result = response.json()
                logger.info(f"카카오 관리자 권한으로 사용자 연결 해제 성공: {result.get('id')}")
                return True
                
            except httpx.HTTPStatusError as e:
                logger.error(f"카카오 Admin API 호출 실패: {e.response.status_code} - {e.response.text}")
                if e.response.status_code == 401:
                    raise ValueError("KAKAO_ADMIN_KEY가 유효하지 않습니다")
                elif e.response.status_code == 404:
                    raise ValueError("카카오 사용자를 찾을 수 없습니다")
                else:
                    raise ValueError(f"카카오 API 오류: {e.response.status_code}")
            except Exception as e:
                logger.error(f"카카오 관리자 권한 사용자 연결 해제 실패: {str(e)}")
                return False

kakao_service = KakaoService()