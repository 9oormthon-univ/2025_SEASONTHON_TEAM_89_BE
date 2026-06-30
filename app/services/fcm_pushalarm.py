"""
FCM(Firebase Cloud Messaging) 푸시 — 안드로이드용.

기존 APNs(pushalarm.py)와 짝을 이루는 송신기. iOS는 APNs, 안드로이드는 FCM으로
보내야 하는데 두 토큰은 호환되지 않으므로, device_token 형식으로 플랫폼을 판별한다
(APNs 토큰은 순수 hex, FCM 토큰은 ':'/'_'/'-' 포함).

firebase-admin / 서비스계정 키가 없으면 자동 비활성화되고, 앱·APNs는 정상 동작한다.
"""

import os
import re
import asyncio
import logging

from app import FIREBASE_CREDENTIALS_PATH

logger = logging.getLogger(__name__)

# firebase-admin은 선택 의존성처럼 다룬다(미설치 시 앱은 정상 기동).
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    _FIREBASE_IMPORTABLE = True
except ImportError:  # pragma: no cover
    firebase_admin = None
    credentials = None
    messaging = None
    _FIREBASE_IMPORTABLE = False


def is_apns_token(token: str) -> bool:
    """
    iOS APNs 디바이스 토큰 여부.

    iOS AppDelegate가 `%02.2hhx`로 만들어 보내므로 APNs 토큰은 순수 16진수다.
    FCM 등록 토큰은 항상 ':'(및 '_','-')를 포함하므로 순수 hex가 아니다.
    """
    if not token:
        return False
    return re.fullmatch(r"[0-9a-fA-F]{32,}", token) is not None


class FCMPusher:
    """firebase-admin(HTTP v1)로 FCM 푸시를 전송. (레거시 서버키 미사용)"""

    def __init__(self):
        self._app = None
        self.ready = False

    def init(self):
        """앱/서비스 최초 import 시 1회 초기화. 키 없으면 비활성화."""
        if self.ready:
            return
        if not _FIREBASE_IMPORTABLE:
            logger.warning("[FCM] firebase-admin 미설치 — FCM 비활성화 (pip install firebase-admin)")
            return
        if not FIREBASE_CREDENTIALS_PATH or not os.path.exists(FIREBASE_CREDENTIALS_PATH):
            logger.warning("[FCM] 서비스 계정 키 없음(%s) — FCM 비활성화", FIREBASE_CREDENTIALS_PATH)
            return
        try:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            # 이미 default app이 있으면 재사용
            try:
                self._app = firebase_admin.get_app()
            except ValueError:
                self._app = firebase_admin.initialize_app(cred)
            self.ready = True
            logger.info("[FCM] 초기화 완료")
        except Exception as e:  # pragma: no cover
            logger.error("[FCM] 초기화 실패: %s", e)

    async def send_notification(self, device_token: str, body: str,
                                from_user_id: str = None, level: str = None,
                                title: str = None) -> bool:
        """
        단일 안드로이드 기기로 FCM 푸시.

        notification(표시) + data(type=family_alert, from_user_id, level)를 함께 전송.
        firebase-admin은 동기 라이브러리라 to_thread로 감싼다. 성공 여부 반환.
        """
        if not self.ready:
            logger.warning("[FCM] 미초기화 — 전송 생략")
            return False

        data = {"type": "family_alert"}
        if from_user_id is not None:
            data["from_user_id"] = str(from_user_id)
        if level is not None:
            data["level"] = str(level)

        message = messaging.Message(
            token=device_token,
            notification=messaging.Notification(title=title or "위허메 가족 알림", body=body),
            data=data,
            android=messaging.AndroidConfig(priority="high"),
        )

        try:
            msg_id = await asyncio.to_thread(messaging.send, message)
            logger.info("[FCM] 전송 성공 token=%s… id=%s", device_token[:8], msg_id)
            return True
        except Exception as e:
            logger.warning("[FCM] 전송 실패 token=%s… err=%s", device_token[:8], e)
            return False


# 싱글톤 인스턴스
fcm_pusher = FCMPusher()
