class Config(object):
    # Setting
    WEB_HOST = 'localhost'
    WEB_PORT = 5000

    # Ollama
    OLLAMA_URL = 'http://localhost:11434'
    OLLAMA_MODEL = 'gemma3:4b'

    # Push Alarm
    AUTH_KEY_PATH = '' # .p8 파일의 경로
    TEAM_ID = '' # Apple Developer Team ID
    AUTH_KEY_ID = '' # .p8 인증 키 ID
    APP_BUNDLE_ID = '' # 앱의 Bundle ID
    IS_PRODUCTION = True # 프로덕션 환경 여부