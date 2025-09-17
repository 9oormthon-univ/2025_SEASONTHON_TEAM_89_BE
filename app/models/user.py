from sqlalchemy import Column, BigInteger, String, DateTime, Boolean, Text
from sqlalchemy.sql import func
from app.core.database import Base

class User(Base):
    """사용자 모델"""
    __tablename__ = "users"
    
    # 기본 ID (내부 사용)
    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    
    # 기본 사용자 정보    # TODO : 타 OAuth 연동시, kakao_id 대체 필요
    kakao_id = Column(String(50), unique=True, index=True, nullable=False, comment="카카오 사용자 ID")
    nickname = Column(String(100), nullable=False, comment="사용자 닉네임")
    
    # 상태 관리
    is_active = Column(Boolean, default=True, comment="활성 상태")
    
    # 시간 정보
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="계정 생성일")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="마지막 수정일")
    last_login_at = Column(DateTime(timezone=True), nullable=True, comment="마지막 로그인 시간")
    
    def __repr__(self):
        return f"<User(id={self.id}, kakao_id={self.kakao_id}, nickname={self.nickname})>"