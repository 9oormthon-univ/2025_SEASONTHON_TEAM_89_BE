# Android 광고 제거 결제 서버

이 변경은 `com.weheome.app`의 월 990원, 연 9,900원, 평생 20,000원 광고 제거 이용권을 Google Play에서 검증한다. **코드 준비와 운영 활성화는 별개다. 기본값은 판매 비활성화이며, 이번 작업에서 운영 서버 배포·계정 권한 변경·운영 비밀키 발급·실제 결제를 수행하지 않았다.**

2026-10-13 공개 운영 API를 읽어 확인한 결과 `/api/billing/*` 경로는 아직 없다. 배포 대상은 개발자의 개인 PC이며, 현재 작업 PC에서 그 서버의 실행 프로세스·컨테이너·배포 설정은 확인되지 않았다. 아래 변경을 실제 서버의 기존 가상환경/실행 경로에 반영해야 한다. 기존 사용자·가족·ML 데이터와 설정은 보존한다.

## 앱과의 계약

세 경로 모두 기존 `require_current_user`의 Bearer JWT로 활성 사용자 계정을 인증한다. 요청 본문의 사용자 ID를 받지 않는다.

- `GET /api/billing/config`: `api_version: 1`, `google_play_ready`, 세 `product_ids`를 반환한다. 비활성화, 누락된 설정/테이블, Google 자격 증명·상품 조회 실패는 `google_play_ready: false`다.
- `GET /api/billing/entitlement`: `user_id`, `verified`, `ad_free`, `product_id`, `expires_at`, `cache_until`을 반환한다. 유효한 Google 검증 결과만 사용하며 임대 기간은 최대 5분이다. 구독은 실제 Google 만료 시간 이내이고, 평생 이용권도 5분마다 새 검증이 필요하다.
- `POST /api/billing/google-play/verify`: `{package_name, product_id, product_type, purchase_token}`를 받고 `verified`, `product_id`, `purchase_token_sha256`, `acknowledged`, `entitlement`를 반환한다. 패키지/상품/유형을 허용 목록으로 확인한다. 완료되지 않은 결제는 승인하지 않고 이용권도 부여하지 않는다.

| 상품 ID | 유형 | 기본 요금제/구매 옵션 | 한국 가격 |
| --- | --- | --- | --- |
| `ad_free_monthly` | `subs` | `monthly`, `P1M` 자동 갱신 | 990원 |
| `ad_free_yearly` | `subs` | `yearly`, `P1Y` 자동 갱신 | 9,900원 |
| `ad_free_lifetime` | `inapp` | `buy`, 비소모성, 다중 수량 없음 | 20,000원 |

서버는 구독에 `purchases.subscriptionsv2.get`, 평생 상품에 최신 `purchases.productsv2.getproductpurchasev2`를 사용한다. 평생 상품 검증 API는 기존 Android 계약의 legacy `products.get`보다 많은 상품/옵션/수량 정보를 제공하지만 앱 응답 계약은 동일하다. [구독 API](https://developers.google.com/android-publisher/api-ref/rest/v3/purchases.subscriptionsv2), [일회성 구매 API](https://developers.google.com/android-publisher/api-ref/rest/v3/purchases.productsv2), [일회성 구매 수명주기](https://developer.android.com/google/play/billing/lifecycle/one-time).

구독은 ACTIVE/IN_GRACE_PERIOD 및 취소했지만 기간이 남은 CANCELED만 허용한다. PENDING/ON_HOLD/PAUSED/EXPIRED는 접근을 허용하지 않는다. 평생 상품은 PURCHASED, `buy` 옵션, 미소비 상태, 수량 1, 환불 가능 수량 1을 요구한다. 대여·예약·할인·알 수 없는 응답은 거절한다.

Google의 obfuscated account ID를 앱과 같은 `SHA-256(인증된 user_id)`로 확인한다. 구매 토큰 해시는 전역 고유 키이고, 소유권은 다른 계정으로 이전하지 않는다. 구매 토큰 원문은 서버 비밀 파일의 Fernet 키로 암호화해 저장하고 오류·로그·HTTP validation 응답에 출력하지 않는다. 계정 삭제 후에도 소유권 기록을 유지해 토큰 재사용을 막는다. 교체 구매가 완료된 경우 이전 토큰을 소유권이 남는 tombstone으로 처리한다. 대기 중 교체 구매 때문에 기존 이용권을 회수하지 않는다. [Google 결제 보안 지침](https://developer.android.com/google/play/billing/security).

**실제 활성화 전 개인정보 처리와 계정 삭제 연동을 정해야 한다.** 새 테이블은 앱 사용자 UUID, account hash, 토큰 hash와 암호화된 구매 토큰, 상품/기간/검증/회수 기록을 저장한다. 현재 코드의 소유권 tombstone은 계정 삭제 후에도 UUID와 암호화된 토큰을 유지하며, 기존 계정 삭제 흐름에 구매 데이터 삭제·축소 처리를 자동으로 추가하지 않았다. 운영자는 개인정보처리방침과 Play Data safety에 실제 구매 이력 처리를 반영하고, 계정 삭제 시 필요한 소유권 해시만 남기는 방식·암호화된 토큰 제거·법적으로 필요한 기록의 최소화와 보관 기간을 결정해 연동/검증해야 한다. 이 문서는 특정 법정 보관 기간이나 무기한 보관 권한을 주장하지 않는다. 삭제/비활성 계정에 RTDN만으로 새 이용권을 부여하지 않는다.

구매 기록을 원자적으로 저장한 뒤 서버에서 승인한다. 승인 실패는 유료 임대 기간을 발급하지 않고 다음 요청/RTDN에서 Google 상태를 다시 확인한다. 같은 계정의 재전송은 멱등 처리한다. 갱신된 구독을 신규 결제로 다시 승인하지 않으며, 평생 상품은 소비하지 않는다.

## 배포 전 필요한 연결

운영자의 실제 서버 경로/실행 사용자/가상환경/재시작 방법을 먼저 확인한다. 이 저장소에는 실제 MySQL·JWT·Kakao 설정이나 서버 접속 키가 없다. 기존 서버 설정을 재사용하고 새 테스트 사용자나 개발용 유료 플래그를 운영 DB에 만들지 않는다.

서버에 다음 설정을 비밀 관리 방식으로 넣는다. 현재 서버의 접근 범위에 맞춰 비밀 파일은 `/home/ajb/9oormthon/backend/.billing-secrets/`에 보관한다. 이 디렉터리는 `.gitignore`의 `/.billing-secrets/`로 Git에서 제외하며, 디렉터리 권한은 `700`, 비밀 파일 권한은 `600`으로 서버 실행 사용자만 읽을 수 있도록 제한한다. 값은 채팅, 커밋, 로그에 넣지 않는다.

| 설정 | 의미 |
| --- | --- |
| `GOOGLE_PLAY_BILLING_ENABLED` | 기본 `false`. 연결/권한/상품/알림/구매 검증 완료 후 운영자가 명시적으로 활성화 |
| `GOOGLE_PLAY_BILLING_TEST_USER_IDS` | 선택 설정. 쉼표로 구분한 위허메 사용자 UUID. 전체 판매 OFF에서도 이 JWT 계정만 구매·복원 가능. Google 라이선스 테스터와 별도 설정 |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_FILE` | Google Play Developer API용 기존 서비스 계정 JSON의 절대 경로 |
| `GOOGLE_PLAY_TOKEN_KEY_FILE` | Fernet 구매 토큰 암호화 키 파일의 절대 경로. 첫 줄은 현재 쓰기 키, 추가 줄은 복호화용 이전 키 |
| `GOOGLE_PLAY_RTDN_AUDIENCE` | Pub/Sub push OIDC 토큰에서 요구할 정확한 audience |
| `GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL` | Pub/Sub 인증된 push 서비스 계정의 정확한 이메일 |
| `GOOGLE_PLAY_RTDN_SUBSCRIPTION` | 정확한 `projects/.../subscriptions/...` 구독 리소스 이름 |

Google Cloud에서 Android Publisher API와 해당 앱의 Play Console 서비스 계정 연결이 필요하다. 서비스 계정에는 이 앱의 상품 조회·주문/구독 조회·구매 승인에 필요한 권한을 부여해야 한다. JSON 파일이 존재하는 것만으로 권한 검증이 완료된 것은 아니다. Pub/Sub push 서비스 계정과 Play 구매 검증 서비스 계정은 역할이 다르다. 이번 코드 작성은 기존 운영 자격 증명을 읽거나 새 운영 키·계정·권한을 생성하지 않았다.

Google Cloud 프로젝트에서 Google Play Developer API를 활성화하고, Play Console의 사용자 및 권한에서 구매 검증 서비스 계정에 해당 앱의 금융 데이터·주문 조회 및 주문·구독 관리 권한을 부여한다. 개발자 계정 자체를 Cloud 프로젝트에 연결하는 예전 절차는 더 이상 필요하지 않다. [공식 API 설정 안내](https://developers.google.com/android-publisher/getting_started).

기존 서버 루트의 `/home/ajb/9oormthon/backend/.env`가 로드되므로 위 결제 환경변수는 여기에 추가하거나 서버 프로세스의 환경으로 전달할 수 있다. 기존 MySQL·JWT 등 다른 설정을 보존한다. 결제 변수가 이미 있다면 그 줄의 경로를 수정하고, 같은 변수 이름을 중복 추가하지 않는다. 구매 검증용 JSON은 Firebase 알림 설정만으로 자동 지정되지 않으며, 실제 Play 권한을 가진 파일을 `GOOGLE_PLAY_SERVICE_ACCOUNT_FILE`에 별도로 지정한다. `GOOGLE_PLAY_TOKEN_KEY_FILE`은 새 구매 토큰 저장을 위한 Fernet 암호화 키 파일이다. 이미 영수증이 저장된 서버에서는 기존 복호화 키를 보존해야 한다. RTDN 값은 실제 설정한 인증된 Pub/Sub push의 audience·서비스 계정·구독 리소스와 일치해야 한다.

### 첫 설정

1. 기존 Google 서비스 계정 JSON이 있다면 그 계정의 이메일과 Cloud 프로젝트를 확인한다. Play Console에서 해당 앱의 구매 검증 권한을 별도로 부여해야 한다. 이 변경을 서버에 반영해 `.billing-secrets/`의 Git 제외 규칙을 먼저 적용하고, 디렉터리를 준비한다.

```bash
cd /home/ajb/9oormthon/backend
mkdir -p .billing-secrets
chmod 700 .billing-secrets
```

JSON 파일을 `/home/ajb/9oormthon/backend/.billing-secrets/play-service-account.json`에 저장한 뒤 파일 권한을 제한한다.

```bash
chmod 600 .billing-secrets/play-service-account.json
```

2. 아래 명령은 `/home/ajb/9oormthon/backend/.billing-secrets/purchase-token.key`를 새로 만든다. 기존 파일은 덮어쓰지 않고 키 원문을 출력하지 않는다. 서버 실행 사용자와 기존 가상환경에서 실행하며, 그 사용자가 이 파일을 읽을 수 있어야 한다.

```bash
python - <<'PY'
import os
from pathlib import Path
from cryptography.fernet import Fernet

directory = Path("/home/ajb/9oormthon/backend/.billing-secrets")
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
directory.chmod(0o700)
filename = directory / "purchase-token.key"
try:
    fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print("기존 암호화 키 파일을 유지합니다.")
else:
    with os.fdopen(fd, "wb") as output:
        output.write(Fernet.generate_key() + b"\n")
    print("암호화 키 파일을 생성했습니다.")
PY
```

3. Google Cloud에서 RTDN topic과 인증된 push subscription을 구성한다. endpoint와 audience는 `https://wiheome.ajb.kr/api/billing/google-play/rtdn`으로 맞출 수 있다. push 인증에 실제 지정한 서비스 계정 이메일과 subscription 리소스 이름을 기록한다. Google Play 알림 발행 계정에 topic의 Pub/Sub Publisher 권한을 부여하고 Play Console의 수익 창출 설정에서 해당 topic을 연결한다. [RTDN 설정](https://developer.android.com/google/play/billing/getting-ready#configure-rtdn), [인증된 push 설정](https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions).
4. `/home/ajb/9oormthon/backend/.env`에서 다음 세 항목을 설정한다. 기존 항목이 있다면 값을 수정하고, 없는 항목만 추가한다. 판매는 `false`로 유지한다. 서버 실행 사용자나 저장 경로가 다르면 실제 절대 경로를 사용한다.

```dotenv
GOOGLE_PLAY_BILLING_ENABLED=false
GOOGLE_PLAY_SERVICE_ACCOUNT_FILE=/home/ajb/9oormthon/backend/.billing-secrets/play-service-account.json
GOOGLE_PLAY_TOKEN_KEY_FILE=/home/ajb/9oormthon/backend/.billing-secrets/purchase-token.key
```

RTDN의 topic·인증된 push subscription·권한·Play 알림 연결을 실제로 구성한 뒤 아래 세 항목을 추가한다. `PROJECT_ID`, push 서비스 계정 이메일과 subscription 이름은 형식을 보여 주는 예시다. **예시 값을 그대로 복사하면 실제 RTDN 인증이 실패한다.** 아직 RTDN을 구성하지 않았다면 임의 값을 넣지 않는다. 이 경우 `check`가 아래 변수들을 `missing_env`로 표시하는 것이 정상이다. `check`의 설정 형식 검사가 통과해도 실제 알림 전달·인증이 검증된 것은 아니다.

```dotenv
GOOGLE_PLAY_RTDN_AUDIENCE=https://wiheome.ajb.kr/api/billing/google-play/rtdn
GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL=push-account@PROJECT_ID.iam.gserviceaccount.com
GOOGLE_PLAY_RTDN_SUBSCRIPTION=projects/PROJECT_ID/subscriptions/weheome-billing-rtdn
```

기존 서버에서 변경을 반영한 뒤 의존성을 설치하고 **서버 관리자가 명시적으로** 새 billing 테이블만 생성한다. 애플리케이션 시작 시 자동 migration은 수행하지 않는다.

```bash
python -m pip install -r requirements.txt
python -m app.billing_ops migrate
python -m app.billing_ops check
```

`check`는 암호화 키, 테이블, 서비스 계정 OAuth 접근과 세 상품의 활성 상태·가격·한국 지역·월/연 기간·평생 구매 옵션을 검증한다. 실패 시 안전한 오류 코드만 출력한다. `checkout_enabled: false` 상태에서 서버 기본 연결을 검증할 수 있다. 코드의 `false` 기본값을 지우거나 요청에서 `verified/ad_free` Boolean을 신뢰하도록 바꾸면 안 된다.

### 설치·테이블 생성 후 `check`가 실패하는 경우

의존성 설치나 `migrate` 성공은 Google Play 연결 준비 완료를 뜻하지 않는다. 판매 OFF는 `check` 실패 원인이 아니다. 결제용 환경변수와 두 비밀 파일, RTDN 설정이 먼저 필요하다. 검사 출력에 표시되는 누락된 변수 이름과 실패 단계를 확인하고 수정한 뒤 같은 가상환경에서 `python -m app.billing_ops check`를 다시 실행한다. 이 명령은 비밀값·실제 파일 경로·Google 응답 원문·예외 원문을 표시하지 않는다.

`stage: configuration`, `reason: missing_environment`이면 `missing_env`의 변수들을 먼저 설정한다. `invalid_fields`는 해당 변수의 절대 경로·파일 존재·읽기 권한·이메일 또는 subscription 형식 문제를 이름과 정해진 reason으로 표시한다. `stage: schema`는 새 billing 테이블, `stage: token_key`는 암호화 키 파일, `stage: catalog`는 Google 접근 또는 활성 상품 조건을 확인해야 한다는 뜻이다. 설정 누락은 DB·Google 연결과 키 내용 읽기 전에 종료한다.

## RTDN과 환불 회수

`POST /api/billing/google-play/rtdn`을 Pub/Sub 인증된 push로 연결한다. 이 경로는 앱 JWT 대신 Google OIDC 서명·issuer·expiry·정확한 audience, 서비스 계정 이메일과 `email_verified`, 정확한 Pub/Sub subscription과 앱 package를 확인한다. 익명 webhook은 없다. 알림 내용만으로 구매 상태를 결정하지 않고 최신 Google API를 조회한다. 성공한 메시지 ID만 DB에 기록하고, 처리 실패는 비-2xx로 응답해 Pub/Sub가 다시 전달하도록 한다. 처음 구매의 RTDN이 앱 구매 검증보다 먼저 와도, 결제 전에 JWT로 등록한 account hash 매핑으로 활성 사용자를 찾아 검증/승인할 수 있다. 매핑이 없는 계정을 새로 생성하지 않는다.

Play Console의 RTDN을 구독/일회성 상품/voided 모두 수신하도록 구성하고 테스트 알림이 이 인증 경로에 도달하는지 확인한다. 운영자가 DLQ와 미승인 결제 모니터링도 구성해야 한다. [Pub/Sub push 인증](https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions), [RTDN 참조](https://developer.android.com/google/play/billing/rtdn-reference).

판매가 켜져 있거나 테스트 사용자 UUID가 설정된 단일-worker API에서는 15분마다 미승인 구매 재시도와 회수 동기화를 서로 다른 DB 세션에서 실행한다. 실패한 영수증은 다음 회차에 재시도하며 다른 영수증의 승인이나 회수 작업을 막지 않는다. 판매 OFF이고 테스트 계정이 없으면 자동 작업은 DB·Google·비밀 파일에 접근하지 않는다. 여러 worker로 늘릴 때는 별도 작업자로 분리하거나 DB 작업 잠금을 추가해야 한다. 수동 회수 동기화도 가능하다. 이 작업은 과거 30일 또는 마지막 성공 지점에서 1시간 중첩해 조회하고 모든 페이지를 처리한다. 커서는 전체 처리가 성공한 뒤에만 전진한다.

```bash
python -m app.billing_ops sync-voided
```

평생 이용권의 수량은 1이므로 검증된 회수는 해당 토큰을 영구 취소한다. RTDN에서도 먼저 Google의 현재 구매 상태를 재조회하며, 회수 알림과 Google 상태가 아직 불일치하면 성공 처리하지 않고 Pub/Sub 재시도를 요청한다. Google Voided Purchases API로 조회한 회수도 현재 구매를 다시 조회한 뒤 처리한다. 구독은 여러 갱신 주문이 같은 토큰을 공유하므로 과거 주문 환불 알림만으로 현재 갱신 이용권을 영구 취소하지 않고 최신 구독 상태를 다시 확인한다. Voided Purchases API는 회수가 필요한 환불만 노출하며 모든 금전 환불 목록이 아니다. [Voided Purchases 안내](https://developers.google.com/android-publisher/voided-purchases).

## 실제 활성화 검증과 제한

운영 서버 연결과 Google 권한/상품/RTDN을 갖춘 뒤 내부 테스트 트랙의 라이선스 계정으로 월·연·평생 구매, 구매 취소, 승인 대기→완료, 앱 종료 후 복원, 다른 기기 복원, 계정 간 토큰 이전 거부, 구독 만료/유예/보류/취소, 환불·회수 및 서버 장애를 확인한다. 유료 임대 기간의 갱신과 모든 광고 종류의 제거도 확인한다. 라이선스 테스트 검증은 실제 Google 응답을 확인해야 하며 아래 offline 테스트는 이를 대체하지 않는다.

이번 구현은 무료 체험·할인·선불·할부·대여·예약·다중 상품·수량 2 이상을 지원하지 않는다. Play 구독센터에서 만료 후 재가입하며 기본 obfuscated account ID가 사라진 영수증은 안전하게 거절한다. 이 경우 `outOfAppPurchaseContext`와 이전 토큰의 검증된 소유권을 복구하는 별도 흐름이 필요하며 앱 내에서 새로 구입하는 경로를 사용한다. 서버 키 교체 시 기존 암호화 키를 읽기 키로 유지해야 기존 영수증을 다시 검증할 수 있다.

로컬 확인 명령:

```bash
python -m pytest -q tests/test_play_billing.py
python -m pytest -q
```

테스트는 격리된 SQLite와 합성 Google 응답만 사용하고 외부 네트워크/운영 DB 접근을 차단한다. 서버의 판매 활성화 값, 운영 사용자, 실제 Google 구매는 테스트에서 변경하지 않는다.

2026-10-13 로컬 검증: billing 보안/오프라인 테스트 99개와 기존 회귀 테스트 75개, 총 **174개 통과** (7.45초). 실제 google-auth 라이브러리의 합성 서비스 계정 서명·OAuth refresh도 MockTransport로 검증했다. 활성 사용자 JWT, 계정 간 토큰 이전, 대기 결제, 승인 실패, 5분 임대 경계, 만료 경계의 자동 갱신, 회수 후 재전송, RTDN 인증/재시도/중복, 구매 토큰 암호화·키 교체를 확인했다. 외부 네트워크·운영 MySQL·실제 Google 구매 호출은 없었다. Python 소스 컴파일과 `git diff --check`도 통과했다. 기존 프레임워크 deprecation 경고 22개는 남아 있으며, 실제 서버 배포·라이선스 구매/알림 검증·개인정보 삭제 연동은 아직 완료되지 않았다.

검증 가상환경의 실제 설치 버전: Python 3.10.12, `google-auth` 2.62.0, `cryptography` 50.0.2, `requests` 2.34.2, `pytest` 9.1.1. `google-auth[requests]`와 `cryptography`는 `requirements.txt`에 위 검증 버전으로 고정했다.

2026-10-14 진단 개선 검증: 설정 누락·잘못된 파일 metadata·단계별 오류 비노출·로그 차단/복구·판매 OFF 점검 성공 등 18개 테스트를 추가했다. 전체 192개 테스트가 통과했고 독립 읽기 검토에서 새로운 문제는 발견되지 않았다. 운영 설정이나 키 파일, 구매 데이터, HTTP 판매 게이트는 변경하지 않았다.


## 2026-10-15 판매 전 보완

계정 삭제는 활성 사용자 행을 잠근 같은 DB 트랜잭션에서 구매 계정 매핑과 암호화 구매 토큰·상품·상태·시각을 제거한다. 토큰 SHA-256과 계정에 재할당할 수 없는 고정 삭제 표식만 남겨 다른 계정의 영수증 재사용을 거부한다. 키 파일이나 Google 접근이 없어도 삭제할 수 있다. 삭제 표식은 알림·회수·승인 재시도에서 복호화하거나 활성화하지 않는다. 계정 삭제와 Google 조회/승인이 겹쳐도 활성 사용자와 최신 구매 행을 재검사한다. 최신 Google 상태 조회부터 구매 저장까지 사용자→구매 행 잠금을 유지한다. 계정 삭제는 Google 구독 취소·환불을 실행하지 않으며 탈퇴 후 새 위허메 계정에서 이전 구매를 복원할 수 없다. 앱 확인 안내와 공개 안내에 같은 정책을 표시한다. CSV 파일 삭제는 기존처럼 DB 밖의 파일 작업이므로 파일·DB 전체 원자성을 보장하지 않는다.

전체 판매는 `GOOGLE_PLAY_BILLING_ENABLED=false`로 유지하면서 테스트할 **위허메 계정 UUID**만 `GOOGLE_PLAY_BILLING_TEST_USER_IDS`에 넣는다. Google Play 이메일을 이 변수에 넣으면 안 된다. 잘못된 UUID 목록은 모든 테스트 허용을 차단한다. 이 제한은 Google 라이선스 테스트를 대신하지 않으므로 Play Console의 라이선스 테스터 등록과 휴대폰 구매창의 테스트 표시·테스트 결제수단도 확인한다. UUID 허용만으로 실결제 비용이 면제되지 않는다. 판매를 켜거나 테스트 UUID를 바꾼 뒤 서버를 재시작한다. 현재 서버 재시작 명령의 명시적 `GOOGLE_PLAY_BILLING_ENABLED=false`는 .env보다 우선하므로 나중에 전체 판매를 켤 때는 그 명령도 변경해야 한다.

```bash
python -m app.billing_ops check
python -m app.billing_ops status
```

`status`는 DB만 읽어 전체 판매 여부, 테스트 계정 수, 마지막 성공 회수 조회 UTC 시각, 활성 계정의 미승인 유효 구매 수를 표시한다. 사용자 UUID·토큰·키·Google 응답은 출력하지 않는다. 동기화가 성공하지 않았으면 시각은 null이다. 운영 로그의 `billing_acknowledgement_retry` 실패 개수 및 `billing_*_failed` 고정 코드를 확인한다. 15분 회차를 계속 실패하면 운영자가 원인을 해결하고 수동 재시도한다. Cloud DLQ/경보는 별도 설정이며 자동 생성하지 않는다.

오프라인 테스트는 운영 DB·네트워크를 차단한 SQLite를 사용한다. 실제 MySQL FOR UPDATE 동시성은 별도 통합 검증 대상이며 라이선스 구매를 대체하지 않는다. 공개 HTML은 Android 저장소의 최신 안내와 동기화했다. 실제 서버/호스팅 배포 완료 여부는 배포 기록을 확인한다.

2026-10-15 최종 로컬 검증: 전체 `tests` **235개 통과**. 판매 대상 제한, 삭제 중 Google 조회·승인 응답, 토큰 최소화/재등록 거부, 최신 상태 재조회, 승인 실패 격리·다음 영수증 처리, 자동 작업 종료 및 읽기 상태 명령을 확인했다. 운영 DB·네트워크 접근 없이 검증했으며 기존 프레임워크 deprecation 경고 24개가 남는다.
