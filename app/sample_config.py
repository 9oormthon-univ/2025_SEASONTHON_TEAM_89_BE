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

    # 가족 알림 빈도 제한 (감지가 잦을 때만 보내기)
    ALERT_THRESHOLD = 3         # 윈도우 내 감지 이 횟수 이상이면 알림 전송
    ALERT_WINDOW_MINUTES = 60   # 감지 집계 윈도우 (분)
    ALERT_COOLDOWN_MINUTES = 60 # 한 번 보낸 뒤 재전송 최소 간격 (분)