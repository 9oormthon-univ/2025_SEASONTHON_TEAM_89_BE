from datetime import datetime, timedelta
from typing import Any, Union, Optional
from jose import jwt, JWTError
from app.config import settings
import logging

logger = logging.getLogger(__name__)

class JWTService:
    """JWT 토큰 서비스"""
    
    def __init__(self):
        self.secret_key = settings.JWT_SECRET_KEY
        self.algorithm = settings.JWT_ALGORITHM
        self.access_token_expire_minutes = settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    
    def create_access_token(
        self, 
        data: dict, 
        expires_delta: Optional[timedelta] = None
    ) -> str:
        """액세스 토큰 생성"""
        to_encode = data.copy()
        
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=self.access_token_expire_minutes)
        
        to_encode.update({
            "exp": expire,
            "iat": datetime.utcnow(),
            "type": "access"
        })
        
        try:
            encoded_jwt = jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)
            logger.info(f"JWT 토큰 생성 성공: user_id={data.get('user_id')}")
            return encoded_jwt
        except Exception as e:
            logger.error(f"JWT 토큰 생성 실패: {str(e)}")
            raise ValueError("토큰 생성 실패")
    
    def verify_token(self, token: str) -> Optional[dict]:
        """토큰 검증 및 페이로드 반환"""
        try:
            payload = jwt.decode(
                token, 
                self.secret_key, 
                algorithms=[self.algorithm]
            )
            
            # 토큰 타입 확인
            if payload.get("type") != "access":
                logger.warning("잘못된 토큰 타입")
                return None
            
            # 만료 시간 확인 (jose에서 자동확인하지만 명시적으로 체크)
            exp = payload.get("exp")
            if exp and datetime.utcnow().timestamp() > exp:
                logger.warning("만료된 토큰")
                return None
            
            logger.info(f"JWT 토큰 검증 성공: user_id={payload.get('user_id')}")
            return payload
            
        except JWTError as e:
            logger.warning(f"JWT 토큰 검증 실패: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"토큰 검증 중 예외 발생: {str(e)}")
            return None
    
    def get_user_id_from_token(self, token: str) -> Optional[int]:
        """토큰에서 사용자 ID 추출"""
        payload = self.verify_token(token)
        if payload:
            return payload.get("user_id")
        return None
    
    def refresh_access_token(self, token: str) -> Optional[str]:
        """액세스 토큰 갱신 (기존 토큰이 유효한 경우)"""
        payload = self.verify_token(token)
        if not payload:
            return None
        
        # 기존 토큰의 사용자 정보로 새 토큰 생성
        new_token_data = {
            "user_id": payload.get("user_id"),
            "kakao_id": payload.get("kakao_id"),
            "nickname": payload.get("nickname")
        }
        
        return self.create_access_token(new_token_data)

jwt_service = JWTService()