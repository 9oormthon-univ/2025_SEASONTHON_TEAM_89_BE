"""PII 마스킹 — 학습 텍스트의 개인정보를 '타입 한국어 토큰'으로 치환. (순수 표준 라이브러리)

목적
 1) 프라이버시/법적 — 사용자 대화로 학습할 때 원문 PII(전화/계좌/주민/카드/이메일/URL/금액)를
    저장·학습하지 않는다(개인정보보호법 + 사용자 동의 전제).
 2) 일반화 — 모델이 특정 숫자를 외우지 않고 "전화번호/계좌번호가 있다"는 패턴을 학습.

플레이스홀더는 KoELECTRA WordPiece에 자연스럽게 토큰화되는 한국어 단어를 쓴다.

⚠️ 학습/추론 parity (중요): 온디바이스 L3 분류기도 **같은 치환을 적용한 텍스트**로 추론해야 한다.
   keyboard/.../analysis/cascade/PiiScrubber.kt 가 이 규칙을 1:1 미러한다 — 규칙을 바꾸면 둘 다 함께 수정.
   (규칙/엔티티 레이어 L1/L2는 원문을 그대로 보므로 영향 없음 — 스크럽은 분류기 입력에만.)
"""
from __future__ import annotations

import re

# 순서 중요: 더 구체적인 패턴(카드/주민/전화)을 계좌·금액보다 먼저. 숫자 경계는 (?<!\d)(?!\d)로.
_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE), "링크"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "이메일"),
    (re.compile(r"(?<!\d)\d{4}-\d{4}-\d{4}-\d{4}(?!\d)"), "카드번호"),
    (re.compile(r"(?<!\d)\d{6}-\d{7}(?!\d)"), "주민번호"),
    (re.compile(r"(?<!\d)01[016789][-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"), "전화번호"),
    (re.compile(r"(?<!\d)1[58]\d{2}[-\s]?\d{4}(?!\d)"), "전화번호"),
    (re.compile(r"(?<!\d)0\d{1,2}[-\s]\d{3,4}[-\s]\d{4}(?!\d)"), "전화번호"),
    (re.compile(r"\d{1,3}(?:,\d{3})+\s*원?|(?<!\d)\d+\s*(?:만원|만\s*원|억|천만원|백만원)"), "금액"),
    (re.compile(r"(?<!\d)\d{2,6}-\d{2,6}-\d{1,7}(?!\d)"), "계좌번호"),
    (re.compile(r"(?<!\d)\d{10,14}(?!\d)"), "계좌번호"),
]


def scrub(text: str) -> str:
    """텍스트의 PII를 타입 토큰으로 치환해 반환. 멱등(이미 치환된 토큰은 다시 안 바뀜)."""
    if not text:
        return text
    for rx, repl in _RULES:
        text = rx.sub(repl, text)
    return text


if __name__ == "__main__":  # 빠른 점검
    samples = [
        "안전계좌 110-234-567890 로 1,200만원 이체하세요",
        "010-1234-5678 로 전화주세요. http://bit.ly/x 클릭",
        "주민번호 900101-1234567 확인",
        "2,100원을 받았어요",
    ]
    for s in samples:
        print(f"{s}  →  {scrub(s)}")
