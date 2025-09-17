from sqlalchemy.orm import Session
from sqlalchemy import and_
from typing import Optional
from datetime import datetime
from app.models.user import User
from app.schemas.kakao import KakaoUserProfile
import logging

logger = logging.getLogger(__name__)

class UserRepository:
    """사용자 데이터 저장소"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_by_kakao_id(self, kakao_id: str) -> Optional[User]:
        """카카오 ID로 사용자 조회"""
        try:
            user = self.db.query(User).filter(
                User.kakao_id == str(kakao_id),
                User.is_active == True
            ).first()
            if user:
                logger.info(f"사용자 조회 성공: kakao_id={kakao_id}")
            return user
        except Exception as e:
            logger.error(f"사용자 조회 실패: kakao_id={kakao_id}, error={str(e)}")
            return None
    
    def get_by_id(self, user_id: int) -> Optional[User]:
        """사용자 ID로 조회"""
        try:
            user = self.db.query(User).filter(
                User.id == user_id,
                User.is_active == True
            ).first()
            if user:
                logger.info(f"사용자 조회 성공: id={user_id}")
            return user
        except Exception as e:
            logger.error(f"사용자 조회 실패: id={user_id}, error={str(e)}")
            return None
    
    def create_user(self, kakao_profile: KakaoUserProfile) -> User:
        """새 사용자 생성"""
        try:
            new_user = User(
                kakao_id=str(kakao_profile.kakao_id),
                nickname=kakao_profile.nickname,
                is_active=True,
                last_login_at=datetime.utcnow()
            )
            
            self.db.add(new_user)
            self.db.commit()
            self.db.refresh(new_user)
            
            logger.info(f"새 사용자 생성 성공: kakao_id={kakao_profile.kakao_id}, id={new_user.id}")
            return new_user
            
        except Exception as e:
            logger.error(f"사용자 생성 실패: kakao_id={kakao_profile.kakao_id}, error={str(e)}")
            self.db.rollback()
            raise
    
    def update_user_profile(self, user: User, kakao_profile: KakaoUserProfile) -> User:
        """사용자 프로필 업데이트"""
        try:
            user.nickname = kakao_profile.nickname
            user.updated_at = datetime.utcnow()
            
            self.db.commit()
            self.db.refresh(user)
            
            logger.info(f"사용자 프로필 업데이트 성공: id={user.id}")
            return user
            
        except Exception as e:
            logger.error(f"사용자 프로필 업데이트 실패: id={user.id}, error={str(e)}")
            self.db.rollback()
            raise
    
    def update_last_login(self, user: User) -> User:
        """마지막 로그인 시간 업데이트"""
        try:
            user.last_login_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(user)
            
            logger.info(f"마지막 로그인 시간 업데이트: id={user.id}")
            return user
            
        except Exception as e:
            logger.error(f"마지막 로그인 시간 업데이트 실패: id={user.id}, error={str(e)}")
            self.db.rollback()
            raise
    
    def deactivate_user(self, user: User) -> User:
        """사용자 탈퇴"""
        try:
            user.is_active = False
            user.updated_at = datetime.utcnow()
            
            self.db.commit()
            self.db.refresh(user)
            
            logger.info(f"사용자 비활성화 성공: id={user.id}")
            return user
            
        except Exception as e:
            logger.error(f"사용자 비활성화 실패: id={user.id}, error={str(e)}")
            self.db.rollback()
            raise
    
    def get_or_create_user(self, kakao_profile: KakaoUserProfile) -> tuple[User, bool]:
        """사용자 조회 또는 생성 (is_new_user 반환)"""
        try:
            # 기존 사용자 조회
            existing_user = self.get_by_kakao_id(str(kakao_profile.kakao_id))
            
            if existing_user:
                # 기존 사용자일 때 프로필 업데이트 및 로그인 시간 갱신
                updated_user = self.update_user_profile(existing_user, kakao_profile)
                self.update_last_login(updated_user)
                return updated_user, False
            else:
                # 신규 사용자 생성
                new_user = self.create_user(kakao_profile)
                return new_user, True
                
        except Exception as e:
            logger.error(f"사용자 조회/생성 실패: kakao_id={kakao_profile.kakao_id}, error={str(e)}")
            raise

def get_user_repository(db: Session) -> UserRepository:
    """사용자 저장소 팩토리"""
    return UserRepository(db)