# config 가이드

config.py 파일을 생성 후 simple_config.py 파일을 참고하여 아래 코드와 같이 작성합니다.

```py
from app.sample_config import Config

class Development(Config):
    WEB_HOST = '0.0.0.0'
    WEB_PORT = 서버 포트 번호

    # Ollama
    OLLAMA_URL = 'OLLAMA 서버 주소'
    OLLAMA_MODEL = 'fraud-detector:latest'

    # Push Alarm
    AUTH_KEY_PATH = '.p8 파일 경로'
    TEAM_ID = 'Apple Developer Team ID'
    AUTH_KEY_ID = '.p8 인증 키 ID'
    APP_BUNDLE_ID = '앱의 Bundle ID'
    IS_PRODUCTION = True | False # 프로덕션 환경 여부
```