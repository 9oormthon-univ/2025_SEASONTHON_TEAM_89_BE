"""
APNs 푸시 알림 서비스

그룹 내 구성원 간 위험 상황 알림 전송 및 알림 설정 관리
"""

import logging
from typing import List, Optional
from datetime import datetime, timedelta
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
# 가족 알림 빈도 제한 설정
from app import ALERT_THRESHOLD, ALERT_WINDOW_MINUTES, ALERT_COOLDOWN_MINUTES
# 안드로이드 FCM 송신기 + 토큰 플랫폼 판별
from app.services.fcm_pushalarm import fcm_pusher, is_apns_token

logger = logging.getLogger(__name__)


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

    def _should_send_alert(self, db: Session, user_id: str, notification_type: str) -> bool:
        """
        감지 빈도 제한: 이번 감지를 기록하고, 윈도우 내 감지가 임계 이상이며
        쿨다운이 지났을 때만 True(전송)를 반환한다.
          - 기본값: ALERT_WINDOW_MINUTES(60분) 내 ALERT_THRESHOLD(3회) 이상 → 전송
          - 한 번 보내면 ALERT_COOLDOWN_MINUTES(60분) 동안 재전송 안 함
        판정 실패(예: detection_events 테이블 없음) 시에는 기존처럼 전송(fail-open).
        """
        try:
            now = datetime.now()
            # 1) 이번 감지 기록
            db.execute(text(
                "INSERT INTO detection_events (user_id, notification_type, occurred_at) "
                "VALUES (:user_id, :type, :now)"
            ), {"user_id": user_id, "type": notification_type, "now": now})
            db.commit()

            # 2) 윈도우 내 감지 횟수
            window_start = now - timedelta(minutes=ALERT_WINDOW_MINUTES)
            row = db.execute(text(
                "SELECT COUNT(*) AS c FROM detection_events "
                "WHERE user_id = :user_id AND notification_type = :type AND occurred_at >= :ws"
            ), {"user_id": user_id, "type": notification_type, "ws": window_start}).fetchone()
            count = row.c if row else 0

            if count < ALERT_THRESHOLD:
                decision, reason = False, f"{count}/{ALERT_THRESHOLD} below threshold"
            else:
                # 3) 쿨다운: 최근에 이미 알림을 보냈으면 스킵
                cooldown_start = now - timedelta(minutes=ALERT_COOLDOWN_MINUTES)
                last = db.execute(text(
                    "SELECT MAX(sent_at) AS last_at FROM notification_logs "
                    "WHERE from_user_id = :user_id AND notification_type = :type"
                ), {"user_id": user_id, "type": notification_type}).fetchone()
                if last and last.last_at and last.last_at >= cooldown_start:
                    decision, reason = False, "in cooldown"
                else:
                    decision, reason = True, f"{count} in {ALERT_WINDOW_MINUTES}min -> send"
        except Exception as e:
            # DB 판정 실패 시에만 fail-open (로깅은 판정에 영향 주지 않음)
            db.rollback()
            logger.warning("[alert-throttle] check failed, fallback=send: %s", e)
            return True

        logger.info("[alert-throttle] %s user=%s: %s (send=%s)",
                    notification_type, user_id, reason, decision)
        return decision

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
            # 1. 발신자의 그룹 정보 확인 (그룹명 포함)
            sender_group = db.execute(text(
                """
                SELECT u.group_id, fg.group_name
                FROM users u
                JOIN family_groups fg ON u.group_id = fg.id
                WHERE u.user_id = :user_id AND u.group_id IS NOT NULL
                """
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

            # 위험 알림 문구 (2줄: 제목 / 본문)
            sender_nickname = sender_info.nickname if sender_info else "구성원"
            group_name = sender_group.group_name or "가족"
            danger_title = f"{group_name}의 {sender_nickname}에게 위험이 감지되었어요"
            danger_body = "지금 바로 연락해서 안전을 확인해 주세요."

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
                    # 위험 푸시 (2줄 문구)
                    success = await self._send_push_notification(
                        device_token=member.device_token,
                        sender_nickname=sender_nickname,
                        danger_type=request.danger_type,
                        message=request.message,
                        from_user_id=request.from_user_id,
                        level="danger",
                        title=danger_title,
                        body_override=danger_body,
                    )
                    
                    if success:
                        sent_count += 1
                    
                    # 알림 기록 저장 (notification_type = 'danger')
                    db.execute(text(
                        """
                        INSERT INTO notification_logs 
                        (from_user_id, to_user_id, group_id, notification_type, message, success, sent_at)
                        VALUES (:from_user_id, :to_user_id, :group_id, :notification_type, :message, :success, :sent_at)
                        """
                    ), {
                        "from_user_id": request.from_user_id,
                        "to_user_id": member.user_id,
                        "group_id": sender_group.group_id,
                        "notification_type": "danger",
                        "message": f"{request.danger_type}: {request.message or ''}",
                        "success": success,
                        "sent_at": datetime.now()
                    })
            
            # 5. 발신자의 위험 횟수 증가
            db.execute(text(
                "UPDATE users SET danger_count = COALESCE(danger_count, 0) + 1 WHERE user_id = :user_id"
            ), {"user_id": request.from_user_id})

            db.commit()

            # 발신자 본인에게도 '가족에게 알림 전송됨' 확인 푸시 (best-effort)
            await self._notify_sender(db, request.from_user_id, "danger", sent_count)

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
                                      level: Optional[str] = None,
                                      title: Optional[str] = None,
                                      body_override: Optional[str] = None):
        """
        디바이스 토큰 형식으로 플랫폼을 판별해 푸시를 분기 전송한다.
          - iOS  (순수 hex 토큰)  → APNs (기존 그대로)
          - 안드로이드 (그 외)     → FCM (firebase-admin)
        title/body_override가 주어지면 그 제목/본문을 그대로 사용(2줄 알림).
        """
        if is_apns_token(device_token):
            return await self._send_apns_notification(
                device_token=device_token,
                sender_nickname=sender_nickname,
                danger_type=danger_type,
                message=message,
                title=title,
                body_override=body_override,
            )
        return await self._send_fcm_notification(
            device_token=device_token,
            sender_nickname=sender_nickname,
            danger_type=danger_type,
            message=message,
            from_user_id=from_user_id,
            level=level,
            title=title,
            body_override=body_override,
        )

    async def _send_fcm_notification(self, device_token: str, sender_nickname: str,
                                     danger_type: str, message: Optional[str],
                                     from_user_id: Optional[str] = None,
                                     level: Optional[str] = None,
                                     title: Optional[str] = None,
                                     body_override: Optional[str] = None):
        """실제 FCM(안드로이드) 푸시 전송."""
        try:
            # 메시지 구성 (body_override가 있으면 그대로 사용)
            if body_override is not None:
                body = body_override
            else:
                body = f"{sender_nickname}님이 {danger_type} 상황입니다."
                if message:
                    body += f" 메시지: {message}"

            success = await self.fcm_pusher.send_notification(
                device_token=device_token,
                body=body,
                from_user_id=from_user_id,
                level=level,
                title=title,
            )
            print(f"[FCM] Notification sent to {device_token}: {success}")
            return success
        except Exception as e:
            print(f"[FCM] Error sending notification to {device_token}: {e}")
            return False

    async def _send_apns_notification(self, device_token: str, sender_nickname: str,
                               danger_type: str, message: Optional[str],
                               title: Optional[str] = None,
                               body_override: Optional[str] = None):
        """실제 APNs 푸시 알림 전송"""
        try:
            # 메시지 구성 (body_override가 있으면 그대로 사용)
            if body_override is not None:
                body = body_override
            else:
                body = f"{sender_nickname}님이 {danger_type} 상황입니다."
                if message:
                    body += f" 메시지: {message}"

            # title이 있으면 2줄(제목/본문), 없으면 단일 문자열
            alert = {"title": title, "body": body} if title else body
            request = NotificationRequest(
                device_token=device_token,
                message={
                    "aps": {
                        "alert": alert,
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

    async def _send_self_push(self, device_token: str, body: str, level: str) -> bool:
        """발신자 본인 기기로 확인용 푸시 (토큰 형식으로 APNs/FCM 분기, 임의 본문)."""
        if is_apns_token(device_token):
            try:
                request = NotificationRequest(
                    device_token=device_token,
                    message={"aps": {"alert": body, "badge": 1}},
                    notification_id=str(uuid4()),
                    time_to_live=3,
                    push_type=PushType.ALERT,
                )
                result = await self.apns_client.send_notification(request)
                return result.is_successful
            except Exception as e:
                print(f"[self-alert][APNs] Error: {e}")
                return False
        return await self.fcm_pusher.send_notification(
            device_token=device_token, body=body, from_user_id=None, level=f"self_{level}"
        )

    async def _notify_sender(self, db: Session, sender_user_id: str, level: str, sent_count: int):
        """그룹원에게 알림이 나갔을 때, 발신자 본인에게도 '전송됨' 확인 푸시. (best-effort)"""
        if sent_count <= 0:
            return
        try:
            row = db.execute(text(
                "SELECT device_token FROM users WHERE user_id = :u AND device_token IS NOT NULL"
            ), {"u": sender_user_id}).fetchone()
            if not row or not row.device_token:
                return
            level_ko = "위험" if level == "danger" else "주의"
            body = f"가족 {sent_count}명에게 {level_ko} 알림을 보냈어요."
            await self._send_self_push(row.device_token, body, level)
        except Exception as e:
            print(f"[self-alert] sender confirm failed (ignored): {e}")
    
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
            # 1. 사용자의 그룹 정보 확인 (그룹명 포함)
            user_group = db.execute(text(
                """
                SELECT u.group_id, fg.group_name
                FROM users u
                JOIN family_groups fg ON u.group_id = fg.id
                WHERE u.user_id = :user_id AND u.group_id IS NOT NULL
                """
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
                    # 위험 푸시 (2줄 문구: 그룹명/닉네임 제목 + 안전 확인 본문)
                    success = await self._send_push_notification(
                        device_token=member.device_token,
                        sender_nickname=user_info.nickname if user_info else "구성원",
                        danger_type="위험 상황",
                        message=f"위험 횟수가 {request.danger_count}회로 증가했습니다",
                        from_user_id=request.user_id,
                        level="danger",
                        title=f"{user_group.group_name or '가족'}의 {user_info.nickname if user_info else '구성원'}에게 위험이 감지되었어요",
                        body_override="지금 바로 연락해서 안전을 확인해 주세요.",
                    )
                    
                    if success:
                        sent_count += 1
                    
                    # 알림 기록 저장 (notification_type = 'danger')
                    db.execute(text(
                        """
                        INSERT INTO notification_logs 
                        (from_user_id, to_user_id, group_id, notification_type, message, success, sent_at)
                        VALUES (:from_user_id, :to_user_id, :group_id, :notification_type, :message, :success, :sent_at)
                        """
                    ), {
                        "from_user_id": request.user_id,
                        "to_user_id": member.user_id,
                        "group_id": user_group.group_id,
                        "notification_type": "danger",
                        "message": f"위험 횟수 증가 (현재: {request.danger_count}회)",
                        "success": success,
                        "sent_at": notification_time
                    })

            db.commit()

            # 발신자 본인에게도 '가족에게 알림 전송됨' 확인 푸시 (best-effort)
            await self._notify_sender(db, request.user_id, "danger", sent_count)

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
            
            # 3. 카운트가 증가했고, 빈도 제한(윈도우 내 임계 이상)을 통과할 때만 자동 알림 발송
            if request.danger_count > current_count.danger_count and \
                    self._should_send_alert(db, request.user_id, "danger"):
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
                        (from_user_id, to_user_id, group_id, notification_type, message, success, sent_at)
                        VALUES (:from_user_id, :to_user_id, :group_id, :notification_type, :message, :success, :sent_at)
                        """
                    ), {
                        "from_user_id": request.user_id,
                        "to_user_id": member.user_id,
                        "group_id": user_group.group_id,
                        "notification_type": "warning",
                        "message": f"경고 횟수 증가 (현재: {request.warning_count}회)",
                        "success": success,
                        "sent_at": notification_time
                    })

            db.commit()

            # 발신자 본인에게도 '가족에게 알림 전송됨' 확인 푸시 (best-effort)
            await self._notify_sender(db, request.user_id, "warning", sent_count)

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
            
            # 3. 주의(warning)는 가족 그룹에 푸시하지 않는다 (위험만 전송).
            #    카운트는 위에서 갱신했고, 그룹 알림은 보내지 않음.

            return True
            
        except Exception as e:
            db.rollback()
            print(f"Error updating warning count: {e}")
            return False
        finally:
            db.close()


# 알림 서비스 인스턴스
notification_service = NotificationService()