import logging
import os
from typing import Optional
from pydantic import BaseModel
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO)

LOGGER = logging.getLogger(__name__)

from app.config import Development as Config

WEB_HOST = Config.WEB_HOST
WEB_PORT = Config.WEB_PORT

OLLAMA_URL = Config.OLLAMA_URL
OLLAMA_MODEL = Config.OLLAMA_MODEL

AUTH_KEY_PATH = Config.AUTH_KEY_PATH
TEAM_ID = Config.TEAM_ID
AUTH_KEY_ID = Config.AUTH_KEY_ID
APP_BUNDLE_ID = Config.APP_BUNDLE_ID
IS_PRODUCTION = Config.IS_PRODUCTION



class Settings(BaseModel):
    # 프로젝트 설정
    PROJECT_NAME: str = "WatchOut"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api"
    DESCRIPTION: str = "위허메요!!!"
    APP_BUNDLE_ID: str = os.getenv("APP_BUNDLE_ID", "com.jaesuneo.WatchOut")
    APP_CUSTOM_SCHEME: str = os.getenv("APP_CUSTOM_SCHEME", "watchout")
    
    # MySQL DB
    MYSQL_SERVER: str = os.getenv("MYSQL_SERVER", "wiheome.ajb.kr")
    MYSQL_USER: str = os.getenv("MYSQL_USER", "watchout_user")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB: str = os.getenv("MYSQL_DB", "watchout_db")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    
    @property
    def DATABASE_URL(self) -> str:
        import urllib.parse
        password = urllib.parse.quote_plus(self.MYSQL_PASSWORD)
        return f"mysql+pymysql://{self.MYSQL_USER}:{password}@{self.MYSQL_SERVER}:{self.MYSQL_PORT}/{self.MYSQL_DB}?charset=utf8mb4"
    
    # 카카오 로그인
    kakao_token_url: str = os.getenv("kakao_token_url", "https://kauth.kakao.com/oauth/token")
    kakao_userInfo_url: str = os.getenv("kakao_userInfo_url", "https://kapi.kakao.com/v2/user/me")
    kakao_client_url: str = os.getenv("kakao_client_url", "")
    redirect_url: str = os.getenv("redirect_url", "http://wiheome.ajb.kr/api/kakao/callback")
    kakao_auth_url: str = os.getenv("kakao_auth_url", "https://kauth.kakao.com/oauth/authorize")
    
    # 새로운 카카오 설정 (호환성을 위해 추가)
    KAKAO_CLIENT_ID: str = os.getenv("KAKAO_CLIENT_ID", "")
    KAKAO_ADMIN_KEY: str = os.getenv("KAKAO_ADMIN_KEY", "")  # 카카오 관리자 키 추가
    KAKAO_REDIRECT_URI: str = os.getenv("KAKAO_REDIRECT_URI", "http://wiheome.ajb.kr/api/kakao/callback")
    
    # JWT 설정 (WatchOut) 
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "WatchOut_super_secret_key_2025_production_change_this")
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "43200"))  # 30일
    
    # 서버 설정
    SERVER_HOST: str = os.getenv("SERVER_HOST", "0.0.0.0")
    SERVER_PORT: int = int(os.getenv("SERVER_PORT", "8000"))
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")
    
    # 로깅 설정
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    SQL_ECHO: bool = os.getenv("SQL_ECHO", "false").lower() == "true"
    
    # 애플 로그인 설정 (향후 사용)
    APPLE_TEAM_ID: Optional[str] = os.getenv("APPLE_TEAM_ID")
    APPLE_KEY_ID: Optional[str] = os.getenv("APPLE_KEY_ID")
    APPLE_BUNDLE_ID: str = os.getenv("APPLE_BUNDLE_ID", "com.jaesuneo.WatchOut")
    APPLE_PRIVATE_KEY_PATH: Optional[str] = os.getenv("APPLE_PRIVATE_KEY_PATH")
    
    # CORS 설정
    BACKEND_CORS_ORIGINS: list = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        # Production
        "https://wiheome.ajb.kr",
    ]

    class Config:
        env_file = ".env"

settings = Settings()