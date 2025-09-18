from uuid import uuid4
from aioapns import APNs, NotificationRequest, PushType

from app import AUTH_KEY_PATH, TEAM_ID, AUTH_KEY_ID, APP_BUNDLE_ID, IS_PRODUCTION


class APNsPusher:
    """
    p8 파일을 이용해 APNs 푸시 알림을 전송하는 클래스
    """
    def __init__(self):

        with open(AUTH_KEY_PATH, 'r') as f:
            auth_key = f.read()

        self.client = APNs(
            key=auth_key,
            key_id=AUTH_KEY_ID,
            team_id=TEAM_ID,
            topic=APP_BUNDLE_ID,
            use_sandbox=not IS_PRODUCTION,
        )

    async def send_notification(self, device_tokens: list | str, body: str) -> bool:
        """
        단일 또는 여러 기기로 푸시 알림을 전송하는 메서드

        Args:
            device_tokens (list or str): 푸시 알림을 받을 기기 토큰 리스트 또는 단일 토큰
            body (str): 알림 내용
        
        리턴값은 성공 여부
        """
        if isinstance(device_tokens, str):
            device_tokens = [device_tokens]

        for token in device_tokens:
            request = NotificationRequest(
                device_token=token,
                message={
                    "aps": {
                        "alert": body,
                        "badge": 1,
                    }
                },
                notification_id=str(uuid4()),
                time_to_live=3,
                push_type=PushType.ALERT,
            )

            res = await self.client.send_notification(request)
            
            return res.is_successful