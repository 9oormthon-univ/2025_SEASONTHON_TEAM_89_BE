# 위허메, 금융범죄를 예방하는 가장 똑똑한 키보드
<img width="1347" height="298" alt="Slice 1 (3)" src="https://github.com/user-attachments/assets/e7c967dc-a039-4173-8627-7f4d573041fe" />

<p align="center">
<img height="387" alt="Group 1597880445" src="https://github.com/user-attachments/assets/6dc9d80b-a9ba-474a-8ffc-d3c030386382" />
</p>
위 급한 판단 실수의 순간! 
허 위 정보에 흔들리지 않도록! 
메 시지를 감지해! 금융범죄를 예방하는 키보드 기반 서비스

## 시스템 아키텍쳐
<img width="1122" height="313" alt="Slice 2" src="https://github.com/user-attachments/assets/677c64c8-bae1-4200-ad51-6cd78d5216a5" />

## config 가이드

config.py 파일을 생성 후 sample_config.py 파일을 참고하여 아래 코드와 같이 작성합니다.

```py
from app.sample_config import Config

class Development(Config):
    WEB_HOST = '0.0.0.0'
    WEB_PORT = 서버 포트 번호
```

## Ollama 및 Gemma 3:4B 설치 가이드

### 1. Ollama 설치

**Windows:**
```bash
# 1. Ollama 공식 사이트에서 Windows 설치 파일 다운로드
# https://ollama.ai/download/windows

# 2. 다운로드한 .exe 파일 실행하여 설치

# 3. 설치 완료 후 명령 프롬프트/PowerShell에서 확인
ollama --version
```

**macOS:**
```bash
# 1. Homebrew를 사용한 설치
brew install ollama

# 2. 또는 공식 사이트에서 .dmg 파일 다운로드
# https://ollama.ai/download/mac
```


### 2. Gemma3 4B 모델 설치

```bash
# Ollama 서비스 시작
ollama serve

# 새 터미널에서 Gemma 3 4B 모델 다운로드 및 설치
ollama pull gemma3:4b

# 설치 확인
ollama list

```



## 실행법

```sh
python -m app
```

