import random
import string
from datetime import datetime
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.core.database import get_db
from app.schemas.family_group import (
    FamilyGroupCreateRequest, 
    FamilyGroupCreateResponse,
    FamilyGroupJoinRequest,
    FamilyGroupJoinResponse,
    FamilyGroupInfoResponse,
    FamilyMember,
    FamilyGroupKickMemberRequest,
    FamilyGroupKickMemberResponse
)


class FamilyGroupService:
    # 그룹 설정 상수
    # 이후 플랜에 따라 수정해야함
    MAX_MEMBERS = 8  # 그룹 최대 멤버 수
    
    def __init__(self):
        self.db_dependency = get_db
    
    def _get_db(self) -> Session:
        """데이터베이스 세션 가져오기"""
        return next(self.db_dependency())
    
    def _generate_group_id(self) -> str:
        """간단한 그룹 ID 생성 (6자리 랜덤)"""
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    
    def _generate_join_code(self) -> str:
        """10자리 참여 코드 생성 (DB에서 중복 확인)"""
        db = self._get_db()
        
        try:
            while True:
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
                
                # 기존 그룹에서 중복 확인
                existing_group = db.execute(text(
                    "SELECT 1 FROM family_groups WHERE join_code = :code"
                ), {"code": code}).fetchone()
                
                if not existing_group:
                    return code
        finally:
            db.close()
    
    def _sync_notification_settings(self, group_id: str, db: Session):
        """
        그룹 내 모든 멤버 간의 알림 설정을 동기화
        새 멤버가 추가되거나 그룹이 생성되면 자동으로 모든 조합의 설정 생성
        """
        try:
            # 1. 현재 그룹의 모든 멤버 조회
            members = db.execute(text("""
                SELECT user_id FROM group_members WHERE group_id = :group_id
            """), {"group_id": group_id}).fetchall()
            
            member_ids = [m.user_id for m in members]
            
            # 2. 모든 조합에 대해 설정 생성 (자기 자신 제외)
            for user_id in member_ids:
                for target_user_id in member_ids:
                    if user_id != target_user_id:
                        # 이미 설정이 있는지 확인
                        existing = db.execute(text("""
                            SELECT id FROM notification_settings 
                            WHERE user_id = :user_id AND target_user_id = :target_user_id
                        """), {
                            "user_id": user_id,
                            "target_user_id": target_user_id
                        }).fetchone()
                        
                        # 없으면 기본값(enabled=true)으로 생성
                        if not existing:
                            db.execute(text("""
                                INSERT INTO notification_settings (user_id, target_user_id, enabled, created_at, updated_at)
                                VALUES (:user_id, :target_user_id, TRUE, NOW(), NOW())
                            """), {
                                "user_id": user_id,
                                "target_user_id": target_user_id
                            })
            
        except Exception as e:
            print(f"Error syncing notification settings: {e}")
            raise
    
    def create_family_group(self, request: FamilyGroupCreateRequest) -> FamilyGroupCreateResponse:
        """가족 그룹 즉시 생성"""
        db = self._get_db()
        
        try:
            # 1. 사용자가 이미 그룹에 속해있는지 확인 및 사용자 정보 조회
            user_info = db.execute(text(
                "SELECT user_id, group_id FROM users WHERE user_id = :user_id"
            ), {"user_id": request.user_id}).fetchone()
            
            if not user_info:
                raise ValueError("USER_NOT_FOUND")
            
            if user_info.group_id:
                raise ValueError("USER_ALREADY_IN_GROUP")
            
            # 2. 새 그룹 즉시 생성
            group_id = self._generate_group_id()
            join_code = self._generate_join_code()
            created_at = datetime.now()
            
            # 3. family_groups에 삽입
            db.execute(text(
                """
                INSERT INTO family_groups (id, group_name, creator_id, join_code, created_at)
                VALUES (:group_id, :group_name, :creator_id, :join_code, :created_at)
                """
            ), {
                "group_id": group_id,
                "group_name": request.group_name,
                "creator_id": request.user_id,
                "join_code": join_code,
                "created_at": created_at
            })
            
            # 4. 생성자를 그룹 멤버로 추가 (nickname 포함)
            db.execute(text(
                """
                INSERT INTO group_members (group_id, user_id, nickname, is_creator, joined_at)
                VALUES (:group_id, :user_id, :nickname, :is_creator, :joined_at)
                """
            ), {
                "group_id": group_id,
                "user_id": request.user_id,
                "nickname": request.nickname,
                "is_creator": True,
                "joined_at": created_at
            })
            
            # 5. 사용자 테이블에 그룹 ID 업데이트
            db.execute(text(
                "UPDATE users SET group_id = :group_id WHERE user_id = :user_id"
            ), {
                "group_id": group_id,
                "user_id": request.user_id
            })
            
            # 6. 알림 설정 동기화 (자동 생성)
            self._sync_notification_settings(group_id, db)
            
            db.commit()
            
            return FamilyGroupCreateResponse(
                group_id=group_id,
                group_name=request.group_name,
                join_code=join_code,
                creator_id=request.user_id,
                created_at=created_at
            )
            
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()
    
    def verify_join_code(self, join_code: str) -> dict:
        """
        그룹 참여 코드 검증
        참여하기 전에 그룹이 유효한지 확인
        """
        db = self._get_db()
        
        try:
            # 코드로 그룹 찾기
            group_info = db.execute(text("""
                SELECT 
                    fg.id,
                    COUNT(gm.user_id) as current_members
                FROM family_groups fg
                LEFT JOIN group_members gm ON fg.id = gm.group_id
                WHERE fg.join_code = :join_code AND fg.is_active = TRUE
                GROUP BY fg.id
                LIMIT 1
            """), {"join_code": join_code}).fetchone()
            
            if not group_info:
                raise ValueError("INVALID_CODE")
            
            # 그룹이 가득 찼는지 확인
            is_full = group_info.current_members >= self.MAX_MEMBERS
            
            if is_full:
                raise ValueError("GROUP_FULL")
            
            # 정상 - 참여 가능
            return {
                "is_valid": True
            }
            
        except ValueError:
            raise
        except Exception as e:
            print(f"Error verifying join code: {e}")
            raise ValueError("VERIFICATION_ERROR")
        finally:
            db.close()
    
    def join_family_group(self, request: FamilyGroupJoinRequest) -> FamilyGroupJoinResponse:
        """가족 그룹 참여"""
        db = self._get_db()
        
        try:
            # 1. 사용자가 이미 그룹에 속해있는지 확인 및 사용자 정보 조회
            user_info = db.execute(text(
                "SELECT user_id, group_id FROM users WHERE user_id = :user_id"
            ), {"user_id": request.user_id}).fetchone()
            
            if not user_info:
                raise ValueError("USER_NOT_FOUND")
            
            if user_info.group_id:
                raise ValueError("USER_ALREADY_IN_GROUP")
            
            # 2. 그룹 존재 확인 및 현재 멤버 수 체크
            group_info = db.execute(text(
                "SELECT id, creator_id, current_members FROM family_groups WHERE join_code = :join_code"
            ), {"join_code": request.join_code}).fetchone()
            
            if not group_info:
                raise ValueError("INVALID_JOIN_CODE")
            
            # 최대 멤버 수 체크
            if group_info.current_members >= self.MAX_MEMBERS:
                raise ValueError("GROUP_FULL")
            
            # 3. 이미 그룹에 속해있는지 확인
            existing_member = db.execute(text(
                "SELECT 1 FROM group_members WHERE group_id = :group_id AND user_id = :user_id"
            ), {
                "group_id": group_info.id,
                "user_id": request.user_id
            }).fetchone()
            
            if existing_member:
                raise ValueError("USER_ALREADY_IN_GROUP")
            
            # 4. 그룹에 멤버 추가 (nickname 포함)
            joined_at = datetime.now()
            
            db.execute(text(
                """
                INSERT INTO group_members (group_id, user_id, nickname, joined_at)
                VALUES (:group_id, :user_id, :nickname, :joined_at)
                """
            ), {
                "group_id": group_info.id,
                "user_id": request.user_id,
                "nickname": request.nickname,
                "joined_at": joined_at
            })
            
            # 6. 사용자 테이블에 그룹 ID 업데이트
            db.execute(text(
                "UPDATE users SET group_id = :group_id WHERE user_id = :user_id"
            ), {
                "group_id": group_info.id,
                "user_id": request.user_id
            })
            
            # 7. 알림 설정 동기화 (새 멤버와 기존 멤버들 간의 설정 자동 생성)
            self._sync_notification_settings(group_info.id, db)
            
            db.commit()
            
            return FamilyGroupJoinResponse(
                group_id=group_info.id,
                joined_at=joined_at
            )
            
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()
    
    def get_family_group_info(self, user_id: str) -> Optional[FamilyGroupInfoResponse]:
        """사용자의 가족 그룹 정보 조회 (알림 설정 포함)"""
        db = self._get_db()
        
        try:
            # 1. 사용자의 그룹 ID 확인
            user_group = db.execute(text(
                "SELECT group_id FROM users WHERE user_id = :user_id"
            ), {"user_id": user_id}).fetchone()
            
            if not user_group or not user_group.group_id:
                return None
            
            # 2. 그룹 정보 조회 (is_active 상태도 함께 조회)
            group_info = db.execute(text(
                "SELECT id, group_name, creator_id, join_code, created_at, is_active FROM family_groups WHERE id = :group_id"
            ), {"group_id": user_group.group_id}).fetchone()
            
            if not group_info:
                # 그룹이 삭제됨 - 사용자 테이블의 group_id도 정리
                db.execute(text(
                    "UPDATE users SET group_id = NULL WHERE user_id = :user_id"
                ), {"user_id": user_id})
                db.commit()
                raise ValueError("GROUP_DELETED")
            
            # 그룹이 비활성화된 경우
            if not group_info.is_active:
                raise ValueError("GROUP_DISBANDED")
            
            # 3. 그룹 멤버들 조회
            members_data = db.execute(text(
                """
                SELECT gm.user_id, gm.nickname, gm.joined_at, 
                       (gm.user_id = fg.creator_id) as is_creator,
                       COALESCE(u.warning_count, 0) as warning_count,
                       COALESCE(u.danger_count, 0) as danger_count,
                       u.profile_image
                FROM group_members gm
                JOIN family_groups fg ON gm.group_id = fg.id
                LEFT JOIN users u ON gm.user_id = u.user_id
                WHERE gm.group_id = :group_id
                ORDER BY is_creator DESC, gm.joined_at ASC
                """
            ), {"group_id": group_info.id}).fetchall()
            
            # 4. 요청한 사용자의 알림 설정 조회 (한 번에 모든 멤버에 대해)
            notification_settings = {}
            settings_data = db.execute(text("""
                SELECT target_user_id, enabled
                FROM notification_settings
                WHERE user_id = :user_id
            """), {"user_id": user_id}).fetchall()
            
            for setting in settings_data:
                notification_settings[setting.target_user_id] = setting.enabled
            
            # 5. 멤버 리스트 생성 (알림 설정 포함)
            members = []
            for member in members_data:
                # 자기 자신은 알림 설정이 없으므로 기본값 True
                # 다른 멤버는 설정 조회, 없으면 기본값 True
                notification_enabled = notification_settings.get(member.user_id, True)
                
                members.append(FamilyMember(
                    user_id=member.user_id,
                    nickname=member.nickname,
                    profile_image=member.profile_image,
                    warning_count=member.warning_count,
                    danger_count=member.danger_count,
                    is_creator=bool(member.is_creator),
                    joined_at=member.joined_at,
                    notification_enabled=notification_enabled  # 알림 설정 추가
                ))
            
            return FamilyGroupInfoResponse(
                group_id=group_info.id,
                group_name=group_info.group_name,
                join_code=group_info.join_code,
                creator_id=group_info.creator_id,
                member_count=len(members),
                members=members,
                created_at=group_info.created_at
            )
            
        except Exception as e:
            print(f"Error getting family group info: {e}")
            return None
        finally:
            db.close()
    
    def leave_family_group(self, user_id: str) -> bool:
        """가족 그룹 탈퇴"""
        db = self._get_db()
        
        try:
            # 1. 사용자의 그룹 정보 확인
            user_group = db.execute(text(
                "SELECT group_id FROM users WHERE user_id = :user_id"
            ), {"user_id": user_id}).fetchone()
            
            if not user_group or not user_group.group_id:
                return False
            
            # 2. 그룹 정보 확인
            group_info = db.execute(text(
                "SELECT creator_id FROM family_groups WHERE id = :group_id"
            ), {"group_id": user_group.group_id}).fetchone()
            
            if not group_info:
                return False
            
            # 3. 그룹장이 탈퇴하는 경우 - 그룹 해체
            if group_info.creator_id == user_id:
                # 모든 멤버의 group_id를 NULL로 설정
                db.execute(text(
                    "UPDATE users SET group_id = NULL WHERE group_id = :group_id"
                ), {"group_id": user_group.group_id})
                
                # 그룹 멤버 레코드 삭제
                db.execute(text(
                    "DELETE FROM group_members WHERE group_id = :group_id"
                ), {"group_id": user_group.group_id})
                
                # 그룹과 관련된 모든 알림 설정 삭제
                db.execute(text("""
                    DELETE FROM notification_settings 
                    WHERE user_id IN (SELECT user_id FROM users WHERE group_id = :group_id)
                       OR target_user_id IN (SELECT user_id FROM users WHERE group_id = :group_id)
                """), {"group_id": user_group.group_id})
                
                # 그룹 삭제
                db.execute(text(
                    "DELETE FROM family_groups WHERE id = :group_id"
                ), {"group_id": user_group.group_id})
            else:
                # 4. 일반 멤버 탈퇴
                # 해당 사용자와 관련된 알림 설정 삭제
                db.execute(text("""
                    DELETE FROM notification_settings 
                    WHERE user_id = :user_id OR target_user_id = :user_id
                """), {"user_id": user_id})
                
                # 사용자의 group_id를 NULL로 설정
                db.execute(text(
                    "UPDATE users SET group_id = NULL WHERE user_id = :user_id"
                ), {"user_id": user_id})
                
                # 그룹 멤버 레코드 삭제
                db.execute(text(
                    "DELETE FROM group_members WHERE group_id = :group_id AND user_id = :user_id"
                ), {
                    "group_id": user_group.group_id,
                    "user_id": user_id
                })
            
            db.commit()
            return True
            
        except Exception as e:
            db.rollback()
            print(f"Error leaving family group: {e}")
            return False
        finally:
            db.close()
    
    def kick_member_from_group(self, request: FamilyGroupKickMemberRequest) -> FamilyGroupKickMemberResponse:
        """그룹에서 멤버 추방 (그룹장만 가능)"""
        db = self._get_db()
        
        try:
            # 1. 그룹장의 그룹 확인
            creator_group = db.execute(text(
                """
                SELECT fg.id, fg.creator_id 
                FROM family_groups fg
                JOIN users u ON u.group_id = fg.id
                WHERE u.user_id = :creator_id AND fg.creator_id = :creator_id
                """
            ), {"creator_id": request.creator_id}).fetchone()
            
            if not creator_group:
                raise ValueError("NOT_GROUP_CREATOR")
            
            # 2. 자기 자신을 추방하려는 경우
            if request.creator_id == request.target_user_id:
                raise ValueError("CANNOT_KICK_YOURSELF")
            
            # 3. 대상 사용자가 그룹에 있는지 확인
            target_member = db.execute(text(
                "SELECT user_name FROM group_members WHERE group_id = :group_id AND user_id = :user_id"
            ), {
                "group_id": creator_group.id,
                "user_id": request.target_user_id
            }).fetchone()
            
            if not target_member:
                raise ValueError("USER_NOT_IN_GROUP")
            
            # 4. 추방 대상과 관련된 알림 설정 삭제
            db.execute(text("""
                DELETE FROM notification_settings 
                WHERE user_id = :user_id OR target_user_id = :user_id
            """), {"user_id": request.target_user_id})
            
            # 5. 그룹에서 멤버 제거
            db.execute(text(
                "DELETE FROM group_members WHERE group_id = :group_id AND user_id = :user_id"
            ), {
                "group_id": creator_group.id,
                "user_id": request.target_user_id
            })
            
            # 6. 사용자 테이블에서 그룹 ID 제거
            db.execute(text(
                "UPDATE users SET group_id = NULL WHERE user_id = :user_id"
            ), {"user_id": request.target_user_id})
            
            # 7. 남은 멤버 수 확인
            remaining_count = db.execute(text(
                "SELECT COUNT(*) as count FROM group_members WHERE group_id = :group_id"
            ), {"group_id": creator_group.id}).fetchone()
            
            db.commit()
            
            return FamilyGroupKickMemberResponse(
                success=True,
                kicked_user_id=request.target_user_id,
                kicked_user_name=target_member.user_name,
                remaining_members=remaining_count.count,
                message=f"{target_member.user_name}님이 그룹에서 제거되었습니다."
            )
            
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()
    
    def update_user_warning_count(self, user_id: str, warning_count: int):
        """사용자 경고 횟수 업데이트"""
        db = self._get_db()
        
        try:
            db.execute(text(
                "UPDATE users SET warning_count = :warning_count WHERE id = :user_id"
            ), {
                "warning_count": warning_count,
                "user_id": user_id
            })
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"Error updating warning count: {e}")
        finally:
            db.close()

    def get_all_groups(self) -> List[dict]:
        """모든 그룹 목록 조회 (관리용)"""
        db = self._get_db()
        
        try:
            groups_data = db.execute(text(
                """
                SELECT fg.id, fg.join_code, fg.creator_id, fg.created_at,
                       COUNT(gm.user_id) as member_count
                FROM family_groups fg
                LEFT JOIN group_members gm ON fg.id = gm.group_id
                GROUP BY fg.id, fg.join_code, fg.creator_id, fg.created_at
                ORDER BY fg.created_at DESC
                """
            )).fetchall()
            
            groups = []
            for group in groups_data:
                groups.append({
                    "group_id": group.id,
                    "join_code": group.join_code,
                    "creator_id": group.creator_id,
                    "created_at": group.created_at.isoformat(),
                    "member_count": group.member_count,
                    "max_members": self.MAX_MEMBERS  # 서비스에서 관리
                })
            
            return groups
            
        except Exception as e:
            print(f"Error getting all groups: {e}")
            return []
        finally:
            db.close()

    def get_user_role_in_group(self, user_id: str) -> dict:
        """사용자의 그룹 내 역할 정보 조회"""
        db = self._get_db()
        
        try:
            # 1. 사용자의 그룹 정보 조회
            user_group_info = db.execute(text(
                """
                SELECT u.group_id, fg.creator_id,
                       (u.user_id = fg.creator_id) as is_creator,
                       gm.nickname, gm.joined_at
                FROM users u
                LEFT JOIN family_groups fg ON u.group_id = fg.id
                LEFT JOIN group_members gm ON u.user_id = gm.user_id AND u.group_id = gm.group_id
                WHERE u.user_id = :user_id
                """
            ), {"user_id": user_id}).fetchone()
            
            if not user_group_info or not user_group_info.group_id:
                return {
                    "status": "no_group",
                    "user_id": user_id,
                    "group_id": None,
                    "is_creator": False,
                    "role": None,
                    "nickname": None,
                    "joined_at": None
                }
            
            return {
                "status": "in_group",
                "user_id": user_id,
                "group_id": user_group_info.group_id,
                "is_creator": bool(user_group_info.is_creator),
                "role": "creator" if user_group_info.is_creator else "member",
                "nickname": user_group_info.nickname,
                "joined_at": user_group_info.joined_at.isoformat() if user_group_info.joined_at else None
            }
            
        except Exception as e:
            print(f"Error getting user role: {e}")
            return {
                "status": "error",
                "user_id": user_id,
                "group_id": None,
                "is_creator": False,
                "role": None,
                "nickname": None,
                "joined_at": None
            }
        finally:
            db.close()


family_group_service = FamilyGroupService()