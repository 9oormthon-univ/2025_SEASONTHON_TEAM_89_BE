from sqlalchemy import Column, BigInteger, String, DateTime, Boolean, Text, Integer
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
    profile_image = Column(Text, nullable=True, comment="프로필 이미지 URL(선택사항)")

    # 그룹 관리 (간단한 6자리 ID로 변경)
    group_id = Column(String(6), nullable=True, index=True, comment="현재 속한 그룹 ID (NULL이면 그룹 없음)")
    
    # 경고/위험 횟수 분리
    warning_count = Column(Integer, default=0, comment="주의 받은 횟수")
    danger_count = Column(Integer, default=0, comment="위험 받은 횟수")
    
    # APNs 푸시 알림
    device_token = Column(String(255), nullable=True, index=True, comment="APNs 디바이스 토큰")

    # 상태 관리
    is_active = Column(Boolean, default=True, comment="활성 상태")
    
    # 시간 정보
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="계정 생성일")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="마지막 수정일")
    last_login_at = Column(DateTime(timezone=True), nullable=True, comment="마지막 로그인 시간")
    
    def __repr__(self):
        return f"<User(id={self.id}, kakao_id={self.kakao_id}, nickname={self.nickname})>"