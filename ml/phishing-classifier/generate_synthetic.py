#!/usr/bin/env python3
"""합성 피싱 데이터 생성기 (순수 표준 라이브러리 — 바로 실행 가능).

실데이터(AI Hub 보이스피싱/스미싱 등)는 승인·다운로드가 필요하므로,
도메인 시나리오 템플릿 + 슬롯 치환으로 라벨링된 학습 데이터를 부트스트랩한다.

설계 의도:
  · 위험: 기관사칭/투자사기/스미싱/메신저피싱/원격제어 — 명백한 패턴
  · 주의: 금전 요구·계좌 공유 등 맥락 모호한 경계 케이스
  · 정상: 일상 대화 + **하드 네거티브**("위험"·"계좌"·"검찰" 단어가 들어가지만 정상)
    → 단순 키워드 매칭 오작동(false positive)을 학습으로 억제

3개 클래스 모두 **슬롯 조합**으로 충분한 다양성을 확보해 균형을 맞춘다
(슬롯 풀이 작으면 dedup 후 샘플 수가 급감 → 클래스 불균형).

실데이터가 준비되면 data/sources/*.jsonl 로 합류시키고 prepare_data.py 가 병합한다.
합성 데이터는 시드일 뿐 — 과의존하면 분포가 편향되므로 실데이터로 대체해 나갈 것.

사용:
  python generate_synthetic.py --per-class 1500 --seed 42 --out data/synthetic.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import random

# ---------------------------------------------------------------------------
# 슬롯 풀 (공통)
# ---------------------------------------------------------------------------
INSTITUTIONS = [
    "서울중앙지검", "서울중앙지방검찰청", "금융감독원", "경찰청 사이버수사대",
    "국세청", "검찰청", "지방검찰청 첨단범죄수사부",
]
TITLES = ["검사", "수사관", "담당 검사", "금융범죄수사관", "담당 수사관"]
CRIMES = ["보이스피싱", "대포통장 개설", "자금세탁", "금융범죄", "명의도용 사건"]
SAFE_ACCOUNTS = ["안전계좌", "국가안전계좌", "보호조치 계좌", "자산보전 계좌", "임시 안전계좌"]
AMOUNTS = ["1,200만원", "500만원", "3천만원", "2,000만원", "800만원", "1억", "350만원", "150만원", "4천만원"]
PERCENTS = ["200%", "300%", "월 30%", "하루 10%", "150%", "주 50%"]
RELATIONS = ["엄마", "아빠", "이모", "삼촌", "누나", "형"]
BANKS = ["국민", "신한", "카카오뱅크", "농협", "우리", "토스", "하나", "기업"]
GIFTCARDS = ["문화상품권", "구글기프트카드", "해피머니", "컬쳐랜드", "구글플레이 기프트카드"]
SUS_URLS = [
    "http://bit.ly/3xK9a", "http://han.gl/abcd", "http://me2.do/xY12",
    "http://14.52.33.9/login", "http://gov-support.top/apply", "http://kakao-pay.xyz/auth",
    "http://naver-event.cc/win", "http://vo.la/zz12",
]
ACCT_NOS = ["110-234-567890", "3333-01-1234567", "123-45-6789012", "356-0123-4567-89", "1002-555-123456"]
# 모두 '에'/'까지' 조사를 자연스럽게 붙일 수 있는 시간 명사 (이중 조사 방지)
TIMES = ["오늘", "내일", "이번 주말", "주말", "다음 주", "이번 주", "모레", "금요일", "토요일", "다음 달"]
PEOPLE = ["민수", "지영", "언니", "동생", "팀장님", "교수님", "현우", "수빈", "친구들", "우리 팀", "재선이", "규민이"]

# ---------------------------------------------------------------------------
# 위험 (danger)
# ---------------------------------------------------------------------------
def gen_danger(rng: random.Random) -> tuple[str, str, str]:
    kind = rng.choice(
        ["authority", "authority", "investment", "smishing", "messenger", "remote"]
    )
    if kind == "authority":
        t = rng.choice([
            "{inst} {title}입니다. 본인 명의 계좌가 {crime}에 연루되어 수사 중입니다.",
            "사건번호 2024-형제-{num} 관련입니다. {inst}에서 연락드립니다. 통화 중 절대 끊지 마세요.",
            "본인 명의가 도용되어 {crime}에 연루됐습니다. 구속을 막으려면 {amount}을 {safe}로 이체하셔야 합니다.",
            "자산 보호를 위해 {amount}을 {safe}로 즉시 이체하세요. 이 사실은 가족에게도 알리지 마십시오.",
            "{inst}입니다. 수사 협조를 위해 OTP 번호와 보안카드 일련번호를 불러주세요.",
            "{title}입니다. {crime} 피의자로 조사가 필요하니 지금 {safe}로 {amount} 이체 후 연락 주세요.",
        ])
        text = t.format(
            inst=rng.choice(INSTITUTIONS), title=rng.choice(TITLES),
            crime=rng.choice(CRIMES), amount=rng.choice(AMOUNTS),
            safe=rng.choice(SAFE_ACCOUNTS), num=rng.randint(1000, 9999),
        )
        return text, "위험", "기관사칭형"
    if kind == "investment":
        t = rng.choice([
            "이번 종목 {pct} 수익 보장합니다. 리딩방 입장하시면 단타 정보 실시간으로 드려요.",
            "원금 보장에 {pct} 확정 수익. 지금 {amount} 입금하시면 자리 확보됩니다.",
            "급등주 정보 무료로 드립니다. {pct} 수익 인증, 지금 코인 정보방 들어오세요.",
            "{pct} 확정 수익 보장하는 VIP 리딩방입니다. 가입비 {amount} 입금하시면 종목 추천드려요.",
        ])
        return t.format(pct=rng.choice(PERCENTS), amount=rng.choice(AMOUNTS)), "위험", "투자사기"
    if kind == "smishing":
        t = rng.choice([
            "[Web발신] 택배 주소가 일치하지 않습니다. 재확인 바랍니다 {url}",
            "[국외발신] 해외에서 카드 승인 {amount}. 본인 아닐 시 즉시 확인 {url}",
            "[정부지원금] 신청 대상자입니다. 오늘까지 확인하세요 {url}",
            "[건강보험] 환급금 조회 대상입니다. 아래 링크에서 신청 {url}",
            "[{bank}은행] 비정상 로그인 감지. 본인 확인 바랍니다 {url}",
        ])
        return t.format(url=rng.choice(SUS_URLS), amount=rng.choice(AMOUNTS), bank=rng.choice(BANKS)), "위험", "스미싱"
    if kind == "messenger":
        t = rng.choice([
            "{rel} 나 폰 액정이 깨져서 컴퓨터로 문자해. 급한데 {amount}만 {bank} 계좌로 보내줄 수 있어?",
            "{rel} 나 지금 폰이 고장나서 인증을 못 해. {gift} 핀번호 좀 불러줄 수 있어?",
            "{rel} 나야, 폰 고장나서 다른 번호로 연락해. 지금 결제할 게 있는데 {amount}만 먼저 보내줘.",
            "{rel} 나 급하게 {amount} 필요한데 {bank}로 보내주면 내일 바로 갚을게. 아빠한테는 비밀로 해줘.",
        ])
        return t.format(
            rel=rng.choice(RELATIONS), amount=rng.choice(AMOUNTS),
            bank=rng.choice(BANKS), gift=rng.choice(GIFTCARDS),
        ), "위험", "메신저피싱"
    # remote
    t = rng.choice([
        "보안 강화를 위해 원격제어 앱(애니데스크)을 설치해 주세요. 화면 공유가 필요합니다.",
        "이 링크를 눌러 보안 앱을 설치하세요 {url}. 설치 후 인증번호를 불러주시면 됩니다.",
        "팀뷰어를 설치하시면 제가 원격으로 안전조치를 해드리겠습니다. 계좌 비밀번호도 입력해 주세요.",
    ])
    return t.format(url=rng.choice(SUS_URLS)), "위험", "원격제어"


# ---------------------------------------------------------------------------
# 주의 (caution) — 맥락 모호한 경계 케이스
# ---------------------------------------------------------------------------
CAUTION_TEMPLATES = [
    "{amount} 어디로 보내면 될까요?",
    "계좌번호 {acct}로 입금 부탁드려요.",
    "지금 좀 급한데 {amount}만 빌려줄 수 있어?",
    "이체할 테니 계좌번호 알려주세요. 금액은 {amount}이요.",
    "혹시 {amount} {time}까지 입금 가능할까요?",
    "그 계좌로 {amount} 보내면 되는 거죠?",
    "{amount} 송금했는데 확인 부탁해요.",
    "{person}한테 {amount} 보내야 하는데 계좌 좀 알려줘.",
    "{time}까지 {amount} 입금해 주시면 됩니다. {acct}",
    "수수료 포함 {amount} 맞나요? 어디로 보낼까요?",
    "{person}한테 {amount} {time}까지 보내야 하는데 계좌 {acct} 맞지?",
    "{time}에 {amount} 송금 예정인데 {acct}로 보내면 될까요?",
]
def gen_caution(rng: random.Random) -> tuple[str, str, str]:
    t = rng.choice(CAUTION_TEMPLATES)
    return t.format(
        amount=rng.choice(AMOUNTS), acct=rng.choice(ACCT_NOS),
        time=rng.choice(TIMES), person=rng.choice(PEOPLE),
    ), "주의", "대출사기형"


# ---------------------------------------------------------------------------
# 정상 (normal) — 일상 + 하드 네거티브
# ---------------------------------------------------------------------------
N_ACT = ["영화 보러", "카페 가서 수다 떨러", "산책하러", "운동하러", "도서관에", "맛집 가러",
         "전시 보러", "쇼핑하러", "노래방", "한강 가러"]
N_FOOD = ["김치찌개", "파스타", "치킨", "떡볶이", "초밥", "마라탕", "삼겹살", "냉면", "비빔밥", "라멘"]
N_TOPIC = ["과제", "회의 자료", "발표 준비", "프로젝트", "운동 계획", "여행 일정", "스터디", "이사 준비"]
NORMAL_TEMPLATES = [
    "{time} {act} 갈래?",
    "{time} {food} 먹을까?",
    "{person}한테 연락 왔어?",
    "{topic} 다 했어?",
    "{time} 시간 괜찮아?",
    "{person}하고 {time} 만나기로 했어",
    "오늘 {food} 먹었는데 진짜 맛있더라",
    "{topic} 같이 하자",
    "{time} 비 온대 우산 챙겨",
    "{person} 생일이라 선물 골라야 해",
    "ㅋㅋㅋ {person} 진짜 웃겨",
    "{time} {act} 가는 거 어때?",
    "{person} 오늘 늦게 와?",
    "{topic} 마감 언제까지였지?",
    "{person}하고 {time}에 {act} 갔다가 {food} 먹기로 했어",
    "{person} 말로는 {topic} {time}까지 끝내야 한대",
]
HN_MEDIA = ["영화", "드라마", "다큐", "예능", "유튜브 영상"]
HN_GAME = ["롤", "배그", "오버워치", "피파", "메이플"]
HN_THING = ["월급", "용돈", "환급금", "보너스", "정산금"]
HN_FIN = ["적금", "예금", "청약", "펀드", "연금저축"]
HARD_NEG_TEMPLATES = [
    "그 {media} 너무 위험한 장면 많아서 깜짝 놀랐어",
    "어제 검찰 나오는 {media} 진짜 재밌더라",
    "내 계좌로 {thing} 들어왔다 ㅎㅎ",
    "{game}에서 그 캐릭터 완전 사기야",
    "그 식당 가격이 사기더라 너무 비싸",
    "은행 가서 {fin} 하나 들었어",
    "이체 완료했어 확인해줘 고마워",
    "비밀번호 또 까먹어서 재설정했네",
    "{topic} 발표에 투자 얘기도 들어가",
    "수익률 좋은 {fin} {person}하고 상의해서 들었어",
    "{thing} 들어와서 {fin} 좀 늘렸어",
]
def gen_normal(rng: random.Random) -> tuple[str, str, str]:
    if rng.random() < 0.6:
        t = rng.choice(NORMAL_TEMPLATES)
        return t.format(time=rng.choice(TIMES), act=rng.choice(N_ACT),
                        food=rng.choice(N_FOOD), person=rng.choice(PEOPLE),
                        topic=rng.choice(N_TOPIC)), "정상", "정상"
    t = rng.choice(HARD_NEG_TEMPLATES)
    return t.format(media=rng.choice(HN_MEDIA), game=rng.choice(HN_GAME),
                    thing=rng.choice(HN_THING), fin=rng.choice(HN_FIN),
                    topic=rng.choice(N_TOPIC), person=rng.choice(PEOPLE)), "정상", "정상"


# ---------------------------------------------------------------------------
# 생성 루프
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-class", type=int, default=1500, help="클래스당 목표 샘플 수")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data/synthetic.jsonl")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    generators = {"위험": gen_danger, "주의": gen_caution, "정상": gen_normal}

    rows: list[dict] = []
    short: dict[str, int] = {}
    for label, fn in generators.items():
        seen: set[str] = set()
        attempts = 0
        while len(seen) < args.per_class and attempts < args.per_class * 80:
            attempts += 1
            text, lab, scam_type = fn(rng)
            if text in seen:
                continue
            seen.add(text)
            rows.append({"text": text, "label": lab, "type": scam_type, "origin": "synthetic"})
        if len(seen) < args.per_class:
            short[label] = len(seen)

    rng.shuffle(rows)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    print(f"wrote {len(rows)} rows -> {args.out}")
    for k in ("정상", "주의", "위험"):
        print(f"  {k}: {counts.get(k, 0)}")
    if short:
        print(f"  [warn] 슬롯 조합 부족으로 목표 미달: {short} "
              f"(템플릿/슬롯을 늘리거나 --per-class 를 낮추세요)")


if __name__ == "__main__":
    main()
