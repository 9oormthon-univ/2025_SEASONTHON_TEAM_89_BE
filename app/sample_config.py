class Config(object):
    # Setting
    WEB_HOST = 'localhost'
    WEB_PORT = 5000

    # Ollama
    OLLAMA_URL = 'http://localhost:11434'
    OLLAMA_MODEL = 'gemma3:4b'

    # Push Alarm (APNs - iOS)
    AUTH_KEY_PATH = '' # .p8 파일의 경로
    TEAM_ID = '' # Apple Developer Team ID
    AUTH_KEY_ID = '' # .p8 인증 키 ID
    APP_BUNDLE_ID = '' # 앱의 Bundle ID
    IS_PRODUCTION = True # 프로덕션 환경 여부

    # Push Alarm (FCM - Android)
    FIREBASE_CREDENTIALS_PATH = '' # Firebase 서비스 계정 .json 경로 (없으면 FCM 비활성화)

    # 가족 알림 빈도 제한 (위험 N번에 한 번 전송)
    ALERT_THRESHOLD = 3         # 위험 감지 이 횟수마다 한 번 그룹 알림 전송