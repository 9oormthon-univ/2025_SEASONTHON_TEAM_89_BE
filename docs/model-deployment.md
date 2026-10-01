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

## 선택: 서버 인증 테스트 계정

이 기능은 **기본 비활성화**다. 배포 코드만 올리거나 앱에서 잠금 UI를 해제하는 것으로 활성화되지 않는다.
`POST /api/auth/test-account`는 `{ "code": "…", "device_token": "…" }` JSON을 받고
일반 사용자 JWT와 `auth_provider: "test"`, `user`, `is_new_user`, `expires_in`을 반환한다.
`device_token`은 선택 사항이다. Android는 취소된 로그인에 기기가 연결되지 않도록 코드를 먼저 검증하고,
사용자가 로그인을 완료한 뒤 기존 인증된 `/api/auth/device-token`으로 기기를 등록한다.
카카오 로그인과 동일한 Bearer 인증·본인 user_id 검증을 사용하며 관리자 권한은 부여하지 않는다.

운영자가 최소 128비트 난수의 재사용 가능한 코드를 생성하고 원문은 비밀 저장소에서만 관리한다.
예를 들어 Python `secrets.token_urlsafe(32)`는 256비트 난수에서 URL-safe ASCII 코드를 생성한다.
서버는 입력 형식(ASCII URL-safe 22~128자)과 해시만 검증하며 입력의 실제 난수 엔트로피를 증명하지 않는다.
코드 원문·JWT·디바이스 토큰을 저장소, 터미널 출력 기록, 프록시 body 로그에 남기지 않는다.
운영 환경 변수에는 코드 문자열의 UTF-8 SHA-256 **소문자 64자리 hex**만 넣는다.

    TEST_ACCOUNT_CODES_JSON={"reviewer1":"<SHA256_HEX>","reviewer2":"<OTHER_SHA256_HEX>"}
    TEST_ACCOUNT_CSV_DIR=/비공개/테스트전용/CSV/폴더

위 값은 형식 예시이며 실제 해시가 아니므로 그대로 적용하면 비활성화된다. 슬롯 이름은
`[A-Za-z0-9_-]` 1~24자, 최대 8개이며 서로 다른 코드 해시여야 한다. 설정 누락·빈 값·잘못된 JSON·
중복 키/해시·형식 오류는 전체 로그인을 503으로 차단한다. 형식에 맞는 틀린 코드는 401,
잘못된/과도한 입력은 원문을 포함하지 않는 422, 시도 제한은 429와 Retry-After를 반환한다.
환경 변경을 서버 프로세스에 반영한 뒤 재시작한다. 슬롯을 제거하거나 전체 설정을 끄면 해당
슬롯의 기존 JWT도 일반 인증 경로에서 거부한다. **해시 교체만으로 기존 JWT가 폐기되지는 않는다.**
긴급 중지는 슬롯 제거 또는 사용자 `is_active=false` 처리 후 재시작으로 수행한다.
비활성 계정은 코드가 맞더라도 자동 재활성화하지 않는다.

슬롯마다 `kakao_id=test-account:<slot>`과 일반 UUID4 사용자 ID를 저장하므로 숫자 카카오 ID와 충돌하지 않는다.
반복 로그인은 기존 사용자 ID를 유지하지만 회원 탈퇴 뒤 재생성은 새 ID를 사용해 과거 JWT가 살아나지 않는다.
토큰 없는 합성 구성원만 별도 UUID5를 사용한다.
첫 생성 때만 합성 테스트 가족과 토큰 없는 합성 구성원 1명을 만들며 실제 사용자 데이터는 복사하지 않는다.
재로그인 시 기존 그룹·카운트·설정을 유지하고, 탈퇴/추방/다른 테스트 그룹 참여를 자동으로 되돌리지 않는다.
두 번째 슬롯도 독립된 테스트 계정이므로 기본 가족에서 탈퇴한 뒤 첫 슬롯의 참여 코드를 이용해
그룹 생성·입장·가족 화면·테스트 기기 간 푸시를 검증할 수 있다. 테스트 계정의 코드/그룹은 실제 가족과 공유하지 않는다.

그룹 검증·참여·조회·역할·추방·탈퇴·알림 설정·발신/자동 알림·발신자 확인 알림은 서비스 계층에서
DB의 마커로 실제/테스트 영역을 양방향 분리한다. 이미 혼합된 그룹도 실패 처리하며, 반대 영역에
동일 디바이스 토큰이 남아 있으면 그 토큰으로 발송하지 않는다. 운영 중 혼합 그룹이 발견되면
일반 탈퇴로 다른 영역의 데이터를 삭제하지 말고 서버 관리자가 별도로 정합성을 복구한다.

테스트 CSV는 기본 `data/test_account_uploads` 또는 TEST_ACCOUNT_CSV_DIR에만 저장한다.
운영 ML_INBOX_DIR와 같거나 서로를 포함하는 폴더는 거부한다. 파일 이름은 `test-account-` 접두사를
사용하고 자동 학습 워커도 이 접두사 파일을 건너뛰므로, 실수로 운영 inbox에 복사해도 학습 대상이 아니다.
테스트 계정 삭제는 테스트 폴더의 본인 CSV·DB 관계만 삭제하고 카카오 unlink를 호출하지 않는다.
인증된 실제 계정의 CSV 저장·삭제·학습 경로는 기존과 같다.

로그인 제한은 프로세스 전체 공유 메모리에 5분당 peer 10회 / 전체 60회, peer 최대 256개로 제한된다.
실패와 성공 모두 횟수를 사용하고 사용자 지정 Forwarded 헤더를 직접 신뢰하지 않는다.
**프로세스 간 공유 Redis 제한은 구현되어 있지 않다.** 여러 worker/서버이면 프록시·게이트웨이에서
이 경로에 대한 공유 전체/IP 제한과 요청 body 크기 제한을 추가하고 신뢰할 프록시 범위를 정확히 설정한다.
공유 제한 없이 worker 수를 늘려 제한이 우회되게 배포하지 않는다. 요청 body/인증정보를 기록하는
APM·프록시 로그도 비활성화 또는 마스킹해야 한다.

출시 심사에 사용하는 경우, 제출한 동일 빌드에서 이 경로와 테스트 계정 접근을 유지하고
Console 앱 액세스 안내에 설정 → 지원·테스트 → 테스트 계정 로그인 경로와 안전하게 관리한
자격증명·테스트 절차를 제공한다. 단순 화면 데모가 아니라 실제 서버 기능을 검증한다.
운영 활성화/외부 접근 확인/실기기 테스트가 끝나기 전에는 계정 접근이 준비됐다고 신고하지 않는다.
