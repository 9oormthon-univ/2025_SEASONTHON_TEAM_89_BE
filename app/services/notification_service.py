"""
APNs 푸시 알림 서비스

그룹 내 구성원 간 위험 상황 알림 전송 및 알림 설정 관리
"""

from typing import List, Optional
from datetime import datetime
from uuid import uuid4
from sqlalchemy.orm import Session
from sqlalchemy import text
from aioapns import APNs, NotificationRequest, PushType

from app.core.database import get_db
from app.schemas.family_group import (
    NotificationSettingRequest,
    NotificationSettingResponse,
    DangerNotificationRequest,
    DangerNotificationResponse
)
from app.config import settings


class NotificationService:
    def __init__(self):
        self.db_dependency = get_db
        self._init_apns_client()
    
    def _init_apns_client(self):
        """APNs 클라이언트 초기화"""
        try:
            # config에서 APNs 설정 가져오기
            auth_key_path = getattr(settings, 'AUTH_KEY_PATH', None)
            team_id = getattr(settings, 'TEAM_ID', None)
            auth_key_id = getattr(settings, 'AUTH_KEY_ID', None)
            app_bundle_id = getattr(settings, 'APP_BUNDLE_ID', 'com.jaesuneo.WatchOut')
            is_production = getattr(settings, 'IS_PRODUCTION', False)
            
            if auth_key_path and team_id and auth_key_id:
                with open(auth_key_path, 'r') as f:
                    auth_key = f.read()
                
                self.apns_client = APNs(
                    key=auth_key,
                    key_id=auth_key_id,
                    team_id=team_id,
                    topic=app_bundle_id,
                    use_sandbox=not is_production,
                )
                print("APNs client initialized successfully")
            else:
                self.apns_client = None
                print("APNs configuration missing, notifications will be logged only")
        except Exception as e:
            self.apns_client = None
            print(f"Failed to initialize APNs client: {e}")
    
    def _get_db(self) -> Session:
        """데이터베이스 세션 가져오기"""
        return next(self.db_dependency())
    
    def update_notification_setting(self, request: NotificationSettingRequest) -> NotificationSettingResponse:
        """특정 구성원에 대한 알림 설정 변경"""
        db = self._get_db()
        
        try:
            # 1. 두 사용자가 같은 그룹에 속해있는지 확인
            same_group = db.execute(text(
                """
                SELECT u1.group_id
                FROM users u1
                JOIN users u2 ON u1.group_id = u2.group_id
                WHERE u1.id = :user_id AND u2.id = :target_user_id 
                AND u1.group_id IS NOT NULL
                """
            ), {
                "user_id": request.user_id,
                "target_user_id": request.target_user_id
            }).fetchone()
            
            if not same_group:
                raise ValueError("USERS_NOT_IN_SAME_GROUP")
            
            # 2. 기존 설정 확인 또는 새로 생성
            existing_setting = db.execute(text(
                """
                SELECT id FROM notification_settings 
                WHERE user_id = :user_id AND target_user_id = :target_user_id
                """
            ), {
                "user_id": request.user_id,
                "target_user_id": request.target_user_id
            }).fetchone()
            
            if existing_setting:
                # 기존 설정 업데이트
                db.execute(text(
                    """
                    UPDATE notification_settings 
                    SET enabled = :enabled, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = :user_id AND target_user_id = :target_user_id
                    """
                ), {
                    "enabled": request.enabled,
                    "user_id": request.user_id,
                    "target_user_id": request.target_user_id
                })
            else:
                # 새 설정 생성
                db.execute(text(
                    """
                    INSERT INTO notification_settings (user_id, target_user_id, enabled)
                    VALUES (:user_id, :target_user_id, :enabled)
                    """
                ), {
                    "user_id": request.user_id,
                    "target_user_id": request.target_user_id,
                    "enabled": request.enabled
                })
            
            db.commit()
            
            return NotificationSettingResponse(
                success=True,
                user_id=request.user_id,
                target_user_id=request.target_user_id,
                enabled=request.enabled,
                message=f"{'활성화' if request.enabled else '비활성화'}되었습니다."
            )
            
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()
    
    async def send_danger_notification(self, request: DangerNotificationRequest) -> DangerNotificationResponse:
        """그룹 내 모든 구성원에게 위험 알림 전송"""
        db = self._get_db()
        
        try:
            # 1. 발신자의 그룹 정보 확인
            sender_group = db.execute(text(
                "SELECT group_id FROM users WHERE id = :user_id AND group_id IS NOT NULL"
            ), {"user_id": request.from_user_id}).fetchone()
            
            if not sender_group:
                raise ValueError("USER_NOT_IN_GROUP")
            
            # 2. 같은 그룹의 모든 구성원 조회 (발신자 제외)
            group_members = db.execute(text(
                """
                SELECT u.id, u.device_token, gm.nickname
                FROM users u
                JOIN group_members gm ON u.id = gm.user_id
                WHERE u.group_id = :group_id AND u.id != :from_user_id
                AND u.device_token IS NOT NULL
                """
            ), {
                "group_id": sender_group.group_id,
                "from_user_id": request.from_user_id
            }).fetchall()
            
            # 3. 발신자 정보 조회
            sender_info = db.execute(text(
                "SELECT gm.nickname FROM group_members gm WHERE gm.user_id = :user_id"
            ), {"user_id": request.from_user_id}).fetchone()
            
            sent_count = 0
            notification_time = datetime.now()
            
            # 4. 각 구성원에게 알림 전송 (알림 설정 확인)
            for member in group_members:
                # 알림 설정 확인 (기본값: True)
                notification_enabled = db.execute(text(
                    """
                    SELECT COALESCE(enabled, TRUE) as enabled 
                    FROM notification_settings 
                    WHERE user_id = :user_id AND target_user_id = :target_user_id
                    """
                ), {
                    "user_id": member.id,
                    "target_user_id": request.from_user_id
                }).fetchone()
                
                if not notification_enabled or notification_enabled.enabled:
                    # APNs 푸시 알림 전송
                    success = await self._send_apns_notification(
                        device_token=member.device_token,
                        sender_nickname=sender_info.nickname if sender_info else "구성원",
                        danger_type=request.danger_type,
                        location=request.location,
                        message=request.message
                    )
                    
                    if success:
                        sent_count += 1
                    
                    # 알림 기록 저장
                    db.execute(text(
                        """
                        INSERT INTO notification_logs 
                        (from_user_id, to_user_id, group_id, danger_type, location, message, sent_at)
                        VALUES (:from_user_id, :to_user_id, :group_id, :danger_type, :location, :message, :sent_at)
                        """
                    ), {
                        "from_user_id": request.from_user_id,
                        "to_user_id": member.id,
                        "group_id": sender_group.group_id,
                        "danger_type": request.danger_type,
                        "location": request.location,
                        "message": request.message,
                        "sent_at": notification_time
                    })
            
            # 5. 발신자의 위험 횟수 증가
            db.execute(text(
                "UPDATE users SET danger_count = COALESCE(danger_count, 0) + 1 WHERE id = :user_id"
            ), {"user_id": request.from_user_id})
            
            db.commit()
            
            return DangerNotificationResponse(
                success=True,
                sent_count=sent_count,
                group_id=sender_group.group_id,
                from_user_id=request.from_user_id,
                timestamp=notification_time
            )
            
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()
    
    async def _send_apns_notification(self, device_token: str, sender_nickname: str, 
                               danger_type: str, location: Optional[str], 
                               message: Optional[str]):
        """실제 APNs 푸시 알림 전송"""
        try:
            title = "⚠️ 위험 상황 발생"
            body = f"{sender_nickname}님이 {danger_type} 상황을 보고했습니다."
            
            if location:
                body += f" 위치: {location}"
            
            if message:
                body += f" 메시지: {message}"
            
            # APNs 클라이언트가 초기화되어 있는 경우 실제 전송
            if self.apns_client:
                request = NotificationRequest(
                    device_token=device_token,
                    message={
                        "aps": {
                            "alert": {
                                "title": title,
                                "body": body
                            },
                            "badge": 1,
                            "sound": "emergency.wav",
                            "category": "DANGER_ALERT"
                        },
                        "custom_data": {
                            "danger_type": danger_type,
                            "from_user": sender_nickname,
                            "location": location,
                            "timestamp": datetime.now().isoformat()
                        }
                    },
                    notification_id=str(uuid4()),
                    time_to_live=3,
                    push_type=PushType.ALERT,
                )
                
                result = await self.apns_client.send_notification(request)
                print(f"[APNs] Notification sent to {device_token}: {result.is_successful}")
                return result.is_successful
            else:
                # APNs 클라이언트가 없는 경우 로그만 출력
                print(f"[APNs] Would send to {device_token}: {title} - {body}")
                return True
                
        except Exception as e:
            print(f"[APNs] Error sending notification to {device_token}: {e}")
            return False
    
    def get_notification_settings(self, user_id: str) -> List[dict]:
        """사용자의 모든 알림 설정 조회"""
        db = self._get_db()
        
        try:
            # 같은 그룹의 모든 구성원에 대한 알림 설정 조회
            settings = db.execute(text(
                """
                SELECT 
                    gm.user_id as target_user_id,
                    gm.nickname as target_nickname,
                    COALESCE(ns.enabled, TRUE) as enabled
                FROM group_members gm
                JOIN users u ON u.group_id = gm.group_id
                LEFT JOIN notification_settings ns ON ns.user_id = :user_id AND ns.target_user_id = gm.user_id
                WHERE u.id = :user_id AND gm.user_id != :user_id
                ORDER BY gm.nickname
                """
            ), {"user_id": user_id}).fetchall()
            
            return [
                {
                    "target_user_id": setting.target_user_id,
                    "target_nickname": setting.target_nickname,
                    "enabled": bool(setting.enabled)
                }
                for setting in settings
            ]
            
        except Exception as e:
            print(f"Error getting notification settings: {e}")
            return []
        finally:
            db.close()


# 알림 서비스 인스턴스
notification_service = NotificationService()