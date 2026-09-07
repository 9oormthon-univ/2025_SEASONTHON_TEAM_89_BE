# 서버 PC 배포: 자동 학습 + 모델 파일 제공

앱 APK는 Firebase App Distribution으로 배포한다. 이 저장소는 CSV를 수신하고, 학습/검증 후
ONNX 모델과 사전 파일을 앱에 배포한다. API 주소는 기존 서버 도메인 그대로 사용한다.

## 최초 실행

저장소 루트에서 기존 서버 가상환경을 활성화한 뒤 실행:

    git pull origin main
    python -m pip install -r requirements.txt -r requirements-ml.txt
    python scripts/setup_ml.py
    python -m app

setup_ml.py는 GitHub의 ml-bootstrap-0.2.14 릴리스에서 현재 앱의 기본 모델과 학습용 HF 체크포인트만
다운로드하고 SHA-256을 검증한다. 사용자 대화·CSV·개인 데이터는 릴리스에 포함하지 않는다.
기존 배포 모델과 데이터는 덮어쓰지 않는다. 기존 모델을 유지하고 새 기록으로 학습하려면
data/ml_assets/data에 아래 4개 비공개 파일을 배치한다.

- train.jsonl / val.jsonl / test.jsonl / golden.jsonl
- 한 줄마다 JSON: {"text": "문장", "label": "정상 또는 주의 또는 위험"}
- train/val은 3개 클래스를 모두 포함해야 한다. test/golden은 학습에 쓰지 않은 검수 데이터를 사용한다.
- 선택: bench_*.jsonl을 같은 폴더에 배치하면 추가 회귀 검증에 포함한다.

실제 검수 데이터가 아직 없다면, 다음 명령으로 합성 초기 데이터를 만들어 기능을 시작할 수 있다:

    python scripts/setup_ml.py --seed-data

합성 데이터 검증은 실제 대화 품질 검증을 대신하지 않는다. 운영 품질 판단에는 별도로 검수한
비공개 평가셋을 배치한다. 기존 데이터가 있으면 --seed-data는 덮어쓰지 않고 중단한다.

## 기존 서버와 자동 연결

- 기존 인증된 POST /api/ml/labeled-csv는 동일하며 CSV를 완전히 기록한 뒤 inbox에 노출한다.
- 서버 lifespan이 1분마다 별도 프로세스로 학습 워커를 호출한다. API 프로세스에는 torch를 로드하지 않는다.
- 기본: 신규 검수 라벨 20개 이상 + 마지막 학습 시도 이후 6시간 이상이면 학습.
- 미검수·중복·상충 라벨 및 평가셋과 겹치는 문장은 제외한다.
- 실제 학습 → ONNX INT8 변환 → 현재 모델과 비교. F1/위험 recall 하락 또는 정상 오탐 증가가
  1%p를 넘으면 배포 거부. 앱에 내려갈 최종 ONNX를 앱과 같은 토크나이저/0.5 규칙으로 평가한다.
- 통과한 모델/vocab만 새 버전으로 게시하고 latest.json을 원자적으로 교체한다.
- 데이터/체크포인트가 없으면 모델 다운로드 API는 동작하고 학습은 대기한다. API 자체가 중단되지는 않는다.
- 여러 API worker를 실행해도 파일 잠금이 동시 학습을 막는다. 서버 종료 시 학습 프로세스 그룹을 종료한다.
- POST 업로드가 성공했다고 해당 기록으로 학습/배포가 이미 끝났다는 뜻은 아니다.

## .env 설정 (모두 선택)

    ML_AUTO_TRAIN=true
    ML_INBOX_DIR=/실제/기존/CSV/저장경로
    ML_TRAIN_DATA_DIR=/비공개/train-val-test-golden/폴더
    ML_MODEL_STATE_DIR=/비공개/학습상태/폴더
    ML_MODEL_PUBLIC_DIR=/모델배포/폴더
    ML_BASE_CHECKPOINT=/학습용/HF체크포인트/폴더
    ML_CHAMPION_DIR=/현재/phishing.onnx-vocab.txt/폴더
    ML_MIN_NEW_LABELS=20
    ML_TRAIN_COOLDOWN_SECONDS=21600
    ML_PYTHON=/학습용/가상환경/bin/python

기본 경로는 저장소 data/ 아래이며 git에서 제외된다. 기존 ML_INBOX_DIR 설정을 그대로 재사용한다.
학습 프로세스는 별도 GPU PC에서 직접 auto_train.py를 실행해도 된다. 이 경우 API 서버의 ML_AUTO_TRAIN=false,
공유 inbox/public 디렉터리를 지정한다. 공개 모델 폴더 안에는 개인정보·학습 데이터를 두지 않는다.

## 모델 API와 앱

- GET /api/ml/models/latest.json: 버전, SHA-256, 크기, 라벨 순서. 초기 설정 전에는 404.
- GET /api/ml/models/releases/{version}/phishing.onnx
- GET /api/ml/models/releases/{version}/vocab.txt
- 다른 파일은 공개하지 않는다. 기존 FastAPI 라우터에 연결되어 별도 8011 포트나 proxy 추가가 필요 없다.
- 서버 외부 HTTPS의 기존 /api/ 경로로 접근 가능해야 한다.
- Firebase 앱 0.2.14 (18)에서 설정 → 사기 감지 엔진 → 지금 모델 업데이트로 확인.
- 자동 업데이트는 기본 ON, 비과금 네트워크/Wi-Fi와 배터리/저장공간 조건에서 24시간 주기로 실행한다.
- 모델 다운로드는 APK 재설치가 아니다. 검증 실패 시 기존 모델 유지, 성공 시 다음 감지부터 적용.

## 운영 확인

    curl -f https://wiheome.ajb.kr/api/ml/models/latest.json
    cat data/ml_state/status.json
    tail -n 50 data/ml_state/worker.log

status.json은 학습을 시작한 뒤 생성된다. training / published / rejected / failed를 확인한다.
자세한 로그는 data/ml_state/runs/<id>/train.log와 evaluation.json에 있다.
이전 모델 폴더와 체크포인트는 보존한다. 디스크 보존 정책을 적용하되 현재 배포/학습 중 파일은 삭제하지 않는다.
기기 기록 제외/삭제는 업로드 이전 선택에 적용되며 이미 서버로 보낸 데이터의 개별 삭제는 별도 API가 필요하다.
