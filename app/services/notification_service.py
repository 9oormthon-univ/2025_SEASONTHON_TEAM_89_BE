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
    DangerNotificationResponse,
    AutoDangerNotificationRequest,
    UpdateDangerCountRequest,
    AutoWarningNotificationRequest,
    UpdateWarningCountRequest
)
# pushalarm.py와 동일한 import 방식 사용
from app import AUTH_KEY_PATH, TEAM_ID, AUTH_KEY_ID, APP_BUNDLE_ID, IS_PRODUCTION
# 안드로이드 FCM 송신기 + 토큰 플랫폼 판별
from app.services.fcm_pushalarm import fcm_pusher, is_apns_token


class NotificationService:
    def __init__(self):
        self.db_dependency = get_db
        self._init_apns_client()
        self._init_fcm_client()

    def _init_fcm_client(self):
        """FCM(firebase-admin) 초기화 - 안드로이드 푸시용. 키 없으면 비활성화."""
        self.fcm_pusher = fcm_pusher
        self.fcm_pusher.init()

    def _init_apns_client(self):
        """APNs 클라이언트 초기화 - pushalarm.py와 동일한 방식"""
        try:
            with open(AUTH_KEY_PATH, 'r') as f:
                auth_key = f.read()
            
            self.apns_client = APNs(
                key=auth_key,
                key_id=AUTH_KEY_ID,
                team_id=TEAM_ID,
                topic=APP_BUNDLE_ID,
                use_sandbox=not IS_PRODUCTION,
            )
            print("APNs client initialized successfully")
        except Exception as e:
            self.apns_client = None
            print(f"Failed to initialize APNs client: {e}")
    
    def _get_db(self) -> Session:
        """데이터베이스 세션 가져오기"""
        return next(self.db_dependency())
    
    def update_notification_setting(self, request: NotificationSettingRequest, notification_type: str = 'danger') -> NotificationSettingResponse:
        """특정 구성원에 대한 알림 설정 변경
        
        Args:
            request: 알림 설정 요청
            notification_type: 'danger' 또는 'warning'
        """
        db = self._get_db()
        
        try:
            # 1. 두 사용자가 같은 그룹에 속해있는지 확인
            same_group = db.execute(text(
                """
                SELECT u1.group_id
                FROM users u1
                JOIN users u2 ON u1.group_id = u2.group_id
                WHERE u1.user_id = :user_id AND u2.user_id = :target_user_id 
                AND u1.group_id IS NOT NULL
                """
            ), {
                "user_id": request.user_id,
                "target_user_id": request.target_user_id
            }).fetchone()
            
            if not same_group:
                raise ValueError("USERS_NOT_IN_SAME_GROUP")
            
            # 2. 알림 유형에 따라 테이블 선택
            table_name = f"{notification_type}_notification_settings"
            
            # 3. 기존 설정 확인 또는 새로 생성
            existing_setting = db.execute(text(
                f"""
                SELECT id FROM {table_name}
                WHERE user_id = :user_id AND target_user_id = :target_user_id
                """
            ), {
                "user_id": request.user_id,
                "target_user_id": request.target_user_id
            }).fetchone()
            
            if existing_setting:
                # 기존 설정 업데이트
                db.execute(text(
                    f"""
                    UPDATE {table_name}
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
                    f"""
                    INSERT INTO {table_name} (user_id, target_user_id, enabled)
                    VALUES (:user_id, :target_user_id, :enabled)
                    """
                ), {
                    "user_id": request.user_id,
                    "target_user_id": request.target_user_id,
                    "enabled": request.enabled
                })
            
            db.commit()
            
            type_label = "위험" if notification_type == "danger" else "경고"
            return NotificationSettingResponse(
                success=True,
                user_id=request.user_id,
                target_user_id=request.target_user_id,
                enabled=request.enabled,
                message=f"{type_label} 알림이 {'활성화' if request.enabled else '비활성화'}되었습니다."
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
                "SELECT group_id FROM users WHERE user_id = :user_id AND group_id IS NOT NULL"
            ), {"user_id": request.from_user_id}).fetchone()
            
            if not sender_group:
                raise ValueError("USER_NOT_IN_GROUP")
            
            # 2. 같은 그룹의 모든 구성원 조회 (발신자 제외)
            group_members = db.execute(text(
                """
                SELECT u.user_id, u.device_token, gm.nickname
                FROM users u
                JOIN group_members gm ON u.user_id = gm.user_id
                WHERE u.group_id = :group_id AND u.user_id != :from_user_id
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
                # 위험 알림 설정 확인 (기본값: True)
                notification_enabled = db.execute(text(
                    """
                    SELECT COALESCE(enabled, TRUE) as enabled 
                    FROM danger_notification_settings
                    WHERE user_id = :user_id AND target_user_id = :target_user_id
                    """
                ), {
                    "user_id": member.user_id,
                    "target_user_id": request.from_user_id
                }).fetchone()
                
                if not notification_enabled or notification_enabled.enabled:
                    # APNs 푸시 알림 전송
                    success = await self._send_push_notification(
                        device_token=member.device_token,
                        sender_nickname=sender_info.nickname if sender_info else "구성원",
                        danger_type=request.danger_type,
                        message=request.message,
                        from_user_id=request.from_user_id,
                        level="danger"
                    )
                    
                    if success:
                        sent_count += 1
                    
                    # 알림 기록 저장 (notification_type = 'danger')
                    db.execute(text(
                        """
                        INSERT INTO notification_logs 
                        (from_user_id, to_user_id, group_id, notification_type, message, sent_at)
                        VALUES (:from_user_id, :to_user_id, :group_id, :notification_type, :message, :sent_at)
                        """
                    ), {
                        "from_user_id": request.from_user_id,
                        "to_user_id": member.user_id,
                        "group_id": sender_group.group_id,
                        "notification_type": "danger",
                        "message": f"{request.danger_type}: {request.message or ''}",
                        "sent_at": datetime.now()
                    })
            
            # 5. 발신자의 위험 횟수 증가
            db.execute(text(
                "UPDATE users SET danger_count = COALESCE(danger_count, 0) + 1 WHERE user_id = :user_id"
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
    
    async def _send_push_notification(self, device_token: str, sender_nickname: str,
                                      danger_type: str, message: Optional[str],
                                      from_user_id: Optional[str] = None,
                                      level: Optional[str] = None):
        """
        디바이스 토큰 형식으로 플랫폼을 판별해 푸시를 분기 전송한다.
          - iOS  (순수 hex 토큰)  → APNs (기존 그대로)
          - 안드로이드 (그 외)     → FCM (firebase-admin)
        """
        if is_apns_token(device_token):
            return await self._send_apns_notification(
                device_token=device_token,
                sender_nickname=sender_nickname,
                danger_type=danger_type,
                message=message,
            )
        return await self._send_fcm_notification(
            device_token=device_token,
            sender_nickname=sender_nickname,
            danger_type=danger_type,
            message=message,
            from_user_id=from_user_id,
            level=level,
        )

    async def _send_fcm_notification(self, device_token: str, sender_nickname: str,
                                     danger_type: str, message: Optional[str],
                                     from_user_id: Optional[str] = None,
                                     level: Optional[str] = None):
        """실제 FCM(안드로이드) 푸시 전송 — APNs와 동일한 본문 구성."""
        try:
            # 메시지 구성 (APNs와 동일)
            body = f"{sender_nickname}님이 {danger_type} 상황입니다."
            if message:
                body += f" 메시지: {message}"

            success = await self.fcm_pusher.send_notification(
                device_token=device_token,
                body=body,
                from_user_id=from_user_id,
                level=level,
            )
            print(f"[FCM] Notification sent to {device_token}: {success}")
            return success
        except Exception as e:
            print(f"[FCM] Error sending notification to {device_token}: {e}")
            return False

    async def _send_apns_notification(self, device_token: str, sender_nickname: str,
                               danger_type: str, message: Optional[str]):
        """실제 APNs 푸시 알림 전송"""
        try:
            # 메시지 구성
            body = f"{sender_nickname}님이 {danger_type} 상황입니다."
            if message:
                body += f" 메시지: {message}"

            request = NotificationRequest(
                device_token=device_token,
                message={
                    "aps": {
                        "alert": body,  # 단순한 문자열 형태
                        "badge": 1,
                    }
                },
                notification_id=str(uuid4()),
                time_to_live=3,
                push_type=PushType.ALERT,
            )
            
            result = await self.apns_client.send_notification(request)
            print(f"[APNs] Notification sent to {device_token}: {result.is_successful}")
            return result.is_successful
                
        except Exception as e:
            print(f"[APNs] Error sending notification to {device_token}: {e}")
            return False
    
    def get_notification_settings(self, user_id: str) -> List[dict]:
        """사용자의 모든 알림 설정 조회 (위험 및 경고)"""
        db = self._get_db()
        
        try:
            # 같은 그룹의 모든 구성원에 대한 알림 설정 조회 (위험 및 경고 분리)
            settings = db.execute(text(
                """
                SELECT 
                    gm.user_id as target_user_id,
                    gm.nickname as target_nickname,
                    COALESCE(dns.enabled, TRUE) as danger_enabled,
                    COALESCE(wns.enabled, TRUE) as warning_enabled
                FROM group_members gm
                JOIN users u ON u.group_id = gm.group_id
                LEFT JOIN danger_notification_settings dns ON dns.user_id = :user_id AND dns.target_user_id = gm.user_id
                LEFT JOIN warning_notification_settings wns ON wns.user_id = :user_id AND wns.target_user_id = gm.user_id
                WHERE u.user_id = :user_id AND gm.user_id != :user_id
                ORDER BY gm.nickname
                """
            ), {"user_id": user_id}).fetchall()
            
            return [
                {
                    "target_user_id": setting.target_user_id,
                    "target_nickname": setting.target_nickname,
                    "danger_enabled": bool(setting.danger_enabled),
                    "warning_enabled": bool(setting.warning_enabled)
                }
                for setting in settings
            ]
            
        except Exception as e:
            print(f"Error getting notification settings: {e}")
            return []
        finally:
            db.close()
    
    async def send_auto_danger_notification(self, request: AutoDangerNotificationRequest) -> DangerNotificationResponse:
        """위험 카운트 증가 시 자동으로 그룹 내 모든 구성원에게 알림 전송"""
        db = self._get_db()
        
        try:
            # 1. 사용자의 그룹 정보 확인
            user_group = db.execute(text(
                "SELECT group_id FROM users WHERE user_id = :user_id AND group_id IS NOT NULL"
            ), {"user_id": request.user_id}).fetchone()
            
            if not user_group:
                raise ValueError("USER_NOT_IN_GROUP")
            
            # 2. 같은 그룹의 모든 구성원 조회 (본인 제외)
            group_members = db.execute(text(
                """
                SELECT u.user_id, u.device_token, gm.nickname
                FROM users u
                JOIN group_members gm ON u.user_id = gm.user_id
                WHERE u.group_id = :group_id AND u.user_id != :user_id
                AND u.device_token IS NOT NULL
                """
            ), {
                "group_id": user_group.group_id,
                "user_id": request.user_id
            }).fetchall()
            
            # 3. 위험 증가한 사용자 정보 조회
            user_info = db.execute(text(
                "SELECT gm.nickname FROM group_members gm WHERE gm.user_id = :user_id"
            ), {"user_id": request.user_id}).fetchone()
            
            sent_count = 0
            notification_time = datetime.now()
            
            # 4. 각 구성원에게 자동 알림 전송
            for member in group_members:
                # 위험 알림 설정 확인 (기본값: TRUE)
                notification_enabled = db.execute(text(
                    """
                    SELECT COALESCE(enabled, TRUE) as enabled 
                    FROM danger_notification_settings
                    WHERE user_id = :user_id AND target_user_id = :target_user_id
                    """
                ), {
                    "user_id": member.user_id,
                    "target_user_id": request.user_id
                }).fetchone()
                
                if not notification_enabled or notification_enabled.enabled:
                    # APNs 푸시 알림 전송
                    success = await self._send_push_notification(
                        device_token=member.device_token,
                        sender_nickname=user_info.nickname if user_info else "구성원",
                        danger_type="위험 상황",
                        message=f"위험 횟수가 {request.danger_count}회로 증가했습니다",
                        from_user_id=request.user_id,
                        level="danger"
                    )
                    
                    if success:
                        sent_count += 1
                    
                    # 알림 기록 저장 (notification_type = 'danger')
                    db.execute(text(
                        """
                        INSERT INTO notification_logs 
                        (from_user_id, to_user_id, group_id, notification_type, message, sent_at)
                        VALUES (:from_user_id, :to_user_id, :group_id, :notification_type, :message, :sent_at)
                        """
                    ), {
                        "from_user_id": request.user_id,
                        "to_user_id": member.user_id,
                        "group_id": user_group.group_id,
                        "notification_type": "danger",
                        "message": f"위험 횟수 증가 (현재: {request.danger_count}회)",
                        "sent_at": notification_time
                    })
            
            db.commit()
            
            return DangerNotificationResponse(
                success=True,
                sent_count=sent_count,
                group_id=user_group.group_id,
                from_user_id=request.user_id,
                timestamp=notification_time
            )
            
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()
    
    async def update_danger_count_with_notification(self, request: UpdateDangerCountRequest) -> bool:
        """위험 카운트 업데이트와 동시에 자동 알림 발송"""
        db = self._get_db()
        
        try:
            # 1 현재 위험 카운트 조회
            current_count = db.execute(text(
                "SELECT danger_count FROM users WHERE user_id = :user_id"
            ), {"user_id": request.user_id}).fetchone()
            
            if not current_count:
                raise ValueError("USER_NOT_FOUND")
            
            # 2. 위험 카운트 업데이트
            db.execute(text(
                "UPDATE users SET danger_count = :danger_count WHERE user_id = :user_id"
            ), {
                "danger_count": request.danger_count,
                "user_id": request.user_id
            })
            
            db.commit()
            
            # 3. 카운트가 증가했을 때만 자동 알림 발송
            if request.danger_count > current_count.danger_count:
                auto_notification = AutoDangerNotificationRequest(
                    user_id=request.user_id,
                    danger_count=request.danger_count
                )
                # 이미 실행 중인 이벤트 루프에서 그대로 await (새 루프 생성 금지)
                # 알림은 best-effort: 그룹 없음/전송 실패해도 카운트 업데이트는 성공 유지
                try:
                    await self.send_auto_danger_notification(auto_notification)
                except Exception as e:
                    print(f"[danger-count] 자동 알림 생략/실패(무시): {e}")
            
            return True
            
        except Exception as e:
            db.rollback()
            print(f"Error updating danger count: {e}")
            return False
        finally:
            db.close()
    
    async def send_auto_warning_notification(self, request: AutoWarningNotificationRequest) -> DangerNotificationResponse:
        """경고 카운트 증가 시 자동으로 그룹 내 모든 구성원에게 알림 전송"""
        db = self._get_db()
        
        try:
            # 1. 사용자의 그룹 정보 확인
            user_group = db.execute(text(
                "SELECT group_id FROM users WHERE user_id = :user_id AND group_id IS NOT NULL"
            ), {"user_id": request.user_id}).fetchone()
            
            if not user_group:
                raise ValueError("USER_NOT_IN_GROUP")
            
            # 2. 같은 그룹의 모든 구성원 조회 (본인 제외)
            group_members = db.execute(text(
                """
                SELECT u.user_id, u.device_token, gm.nickname
                FROM users u
                JOIN group_members gm ON u.user_id = gm.user_id
                WHERE u.group_id = :group_id AND u.user_id != :user_id
                AND u.device_token IS NOT NULL
                """
            ), {
                "group_id": user_group.group_id,
                "user_id": request.user_id
            }).fetchall()
            
            # 3. 경고 증가한 사용자 정보 조회
            user_info = db.execute(text(
                "SELECT gm.nickname FROM group_members gm WHERE gm.user_id = :user_id"
            ), {"user_id": request.user_id}).fetchone()
            
            sent_count = 0
            notification_time = datetime.now()
            
            # 4. 각 구성원에게 자동 알림 전송
            for member in group_members:
                # 경고 알림 설정 확인 (기본값: True)
                notification_enabled = db.execute(text(
                    """
                    SELECT COALESCE(enabled, TRUE) as enabled 
                    FROM warning_notification_settings
                    WHERE user_id = :user_id AND target_user_id = :target_user_id
                    """
                ), {
                    "user_id": member.user_id,
                    "target_user_id": request.user_id
                }).fetchone()
                
                if not notification_enabled or notification_enabled.enabled:
                    # APNs 푸시 알림 전송
                    success = await self._send_push_notification(
                        device_token=member.device_token,
                        sender_nickname=user_info.nickname if user_info else "구성원",
                        danger_type="경고 상황",
                        message=f"경고 횟수가 {request.warning_count}회로 증가했습니다",
                        from_user_id=request.user_id,
                        level="warning"
                    )
                    
                    if success:
                        sent_count += 1
                    
                    # 알림 기록 저장 (notification_type = 'warning')
                    db.execute(text(
                        """
                        INSERT INTO notification_logs 
                        (from_user_id, to_user_id, group_id, notification_type, message, sent_at)
                        VALUES (:from_user_id, :to_user_id, :group_id, :notification_type, :message, :sent_at)
                        """
                    ), {
                        "from_user_id": request.user_id,
                        "to_user_id": member.user_id,
                        "group_id": user_group.group_id,
                        "notification_type": "warning",
                        "message": f"경고 횟수 증가 (현재: {request.warning_count}회)",
                        "sent_at": notification_time
                    })
            
            db.commit()
            
            return DangerNotificationResponse(
                success=True,
                sent_count=sent_count,
                group_id=user_group.group_id,
                from_user_id=request.user_id,
                timestamp=notification_time
            )
            
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()
    
    async def update_warning_count_with_notification(self, request: UpdateWarningCountRequest) -> bool:
        """경고 카운트 업데이트와 동시에 자동 알림 발송"""
        db = self._get_db()
        
        try:
            # 1. 현재 경고 카운트 조회
            current_count = db.execute(text(
                "SELECT warning_count FROM users WHERE user_id = :user_id"
            ), {"user_id": request.user_id}).fetchone()
            
            if not current_count:
                raise ValueError("USER_NOT_FOUND")
            
            # 2. 경고 카운트 업데이트
            db.execute(text(
                "UPDATE users SET warning_count = :warning_count WHERE user_id = :user_id"
            ), {
                "warning_count": request.warning_count,
                "user_id": request.user_id
            })
            
            db.commit()
            
            # 3. 카운트가 증가했을 때만 자동 알림 발송
            if request.warning_count > current_count.warning_count:
                auto_notification = AutoWarningNotificationRequest(
                    user_id=request.user_id,
                    warning_count=request.warning_count
                )
                # 이미 실행 중인 이벤트 루프에서 그대로 await (새 루프 생성 금지)
                # 알림은 best-effort: 그룹 없음/전송 실패해도 카운트 업데이트는 성공 유지
                try:
                    await self.send_auto_warning_notification(auto_notification)
                except Exception as e:
                    print(f"[warning-count] 자동 알림 생략/실패(무시): {e}")
            
            return True
            
        except Exception as e:
            db.rollback()
            print(f"Error updating warning count: {e}")
            return False
        finally:
            db.close()


# 알림 서비스 인스턴스
notification_service = NotificationService()