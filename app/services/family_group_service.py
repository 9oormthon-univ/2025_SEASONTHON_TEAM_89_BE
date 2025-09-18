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
    
    def create_family_group(self, request: FamilyGroupCreateRequest) -> FamilyGroupCreateResponse:
        """가족 그룹 즉시 생성"""
        db = self._get_db()
        
        try:
            # 1. 사용자가 이미 그룹에 속해있는지 확인 및 사용자 정보 조회
            user_info = db.execute(text(
                "SELECT id, group_id FROM users WHERE id = :user_id"
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
                INSERT INTO family_groups (id, creator_id, join_code, created_at)
                VALUES (:group_id, :creator_id, :join_code, :created_at)
                """
            ), {
                "group_id": group_id,
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
                "UPDATE users SET group_id = :group_id WHERE id = :user_id"
            ), {
                "group_id": group_id,
                "user_id": request.user_id
            })
            
            db.commit()
            
            return FamilyGroupCreateResponse(
                group_id=group_id,
                join_code=join_code,
                creator_id=request.user_id,
                created_at=created_at
            )
            
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()
    
    def join_family_group(self, request: FamilyGroupJoinRequest) -> FamilyGroupJoinResponse:
        """가족 그룹 참여"""
        db = self._get_db()
        
        try:
            # 1. 사용자가 이미 그룹에 속해있는지 확인 및 사용자 정보 조회
            user_info = db.execute(text(
                "SELECT id, group_id FROM users WHERE id = :user_id"
            ), {"user_id": request.user_id}).fetchone()
            
            if not user_info:
                raise ValueError("USER_NOT_FOUND")
            
            if user_info.group_id:
                raise ValueError("USER_ALREADY_IN_GROUP")
            
            # 2. 그룹 존재 확인
            group_info = db.execute(text(
                "SELECT id, creator_id FROM family_groups WHERE join_code = :join_code"
            ), {"join_code": request.join_code}).fetchone()
            
            if not group_info:
                raise ValueError("INVALID_JOIN_CODE")
            
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
                "UPDATE users SET group_id = :group_id WHERE id = :user_id"
            ), {
                "group_id": group_info.id,
                "user_id": request.user_id
            })
            
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
        """사용자의 가족 그룹 정보 조회"""
        db = self._get_db()
        
        try:
            # 1. 사용자의 그룹 ID 확인
            user_group = db.execute(text(
                "SELECT group_id FROM users WHERE id = :user_id"
            ), {"user_id": user_id}).fetchone()
            
            if not user_group or not user_group.group_id:
                return None
            
            # 2. 그룹 정보 조회
            group_info = db.execute(text(
                "SELECT id, creator_id, join_code, created_at FROM family_groups WHERE id = :group_id"
            ), {"group_id": user_group.group_id}).fetchone()
            
            if not group_info:
                return None
            
            # 3. 그룹 멤버들 조회
            members_data = db.execute(text(
                """
                SELECT gm.user_id, gm.nickname, gm.joined_at, 
                       (gm.user_id = fg.creator_id) as is_creator,
                       COALESCE(u.warning_count, 0) as warning_count,
                       COALESCE(u.danger_count, 0) as danger_count
                FROM group_members gm
                JOIN family_groups fg ON gm.group_id = fg.id
                LEFT JOIN users u ON gm.user_id = u.id
                WHERE gm.group_id = :group_id
                ORDER BY is_creator DESC, gm.joined_at ASC
                """
            ), {"group_id": group_info.id}).fetchall()
            
            # 4. 멤버 리스트 생성
            members = []
            for member in members_data:
                members.append(FamilyMember(
                    user_id=member.user_id,
                    nickname=member.nickname,
                    warning_count=member.warning_count,
                    danger_count=member.danger_count,
                    is_creator=bool(member.is_creator),
                    joined_at=member.joined_at
                ))
            
            return FamilyGroupInfoResponse(
                group_id=group_info.id,
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
                "SELECT group_id FROM users WHERE id = :user_id"
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
                
                # 그룹 삭제
                db.execute(text(
                    "DELETE FROM family_groups WHERE id = :group_id"
                ), {"group_id": user_group.group_id})
            else:
                # 4. 일반 멤버 탈퇴
                # 사용자의 group_id를 NULL로 설정
                db.execute(text(
                    "UPDATE users SET group_id = NULL WHERE id = :user_id"
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
                WHERE u.id = :creator_id AND fg.creator_id = :creator_id
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
            
            # 4. 그룹에서 멤버 제거
            db.execute(text(
                "DELETE FROM group_members WHERE group_id = :group_id AND user_id = :user_id"
            ), {
                "group_id": creator_group.id,
                "user_id": request.target_user_id
            })
            
            # 5. 사용자 테이블에서 그룹 ID 제거
            db.execute(text(
                "UPDATE users SET group_id = NULL WHERE id = :user_id"
            ), {"user_id": request.target_user_id})
            
            # 6. 남은 멤버 수 확인
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
    
    def get_user_status(self, user_id: str) -> dict:
        """사용자 상태 조회 (폴링용)"""
        group_info = self.get_family_group_info(user_id)
        
        if group_info:
            return {
                "status": "in_group",
                "data": group_info,
                "last_updated": datetime.now().isoformat()
            }
        
        return {
            "status": "no_group",
            "data": None,
            "last_updated": datetime.now().isoformat()
        }
    
    def get_all_groups(self) -> List[dict]:
        """모든 그룹 목록 조회 (관리용)"""
        db = self._get_db()
        
        try:
            groups_data = db.execute(text(
                """
                SELECT fg.id, fg.group_name, fg.join_code, fg.creator_id, fg.created_at,
                       COUNT(gm.user_id) as member_count
                FROM family_groups fg
                LEFT JOIN group_members gm ON fg.id = gm.group_id
                GROUP BY fg.id, fg.group_name, fg.join_code, fg.creator_id, fg.created_at
                ORDER BY fg.created_at DESC
                """
            )).fetchall()
            
            groups = []
            for group in groups_data:
                groups.append({
                    "group_id": group.id,
                    "group_name": group.group_name,
                    "join_code": group.join_code,
                    "creator_id": group.creator_id,
                    "created_at": group.created_at.isoformat(),
                    "member_count": group.member_count
                })
            
            return groups
            
        except Exception as e:
            print(f"Error getting all groups: {e}")
            return []
        finally:
            db.close()


family_group_service = FamilyGroupService()