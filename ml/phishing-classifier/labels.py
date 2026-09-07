"""공유 라벨 정의 — 위허메 피싱 분류기.

앱 쪽 RiskLevel / ScamStage 와 1:1 매핑된다.
  · keyboard/.../model/RiskLevel.kt  → 정상/주의/위험
  · keyboard/.../analysis/cascade/Signal.kt (ScamStage) → 시나리오 단계
"""

# 3-class 위험도 (주 분류 헤드). 인덱스 = 모델 출력 순서.
RISK_LABELS = ["정상", "주의", "위험"]
RISK_TO_ID = {name: i for i, name in enumerate(RISK_LABELS)}
ID_TO_RISK = {i: name for i, name in enumerate(RISK_LABELS)}

# 시나리오 단계 (옵션 멀티헤드). ScamStage 와 매핑.
STAGE_LABELS = ["없음", "사칭", "공포조성", "고립", "행동요구"]
STAGE_TO_ID = {name: i for i, name in enumerate(STAGE_LABELS)}

# 사기 유형 (분석/리포트용 메타데이터 — 학습 타깃 아님)
SCAM_TYPES = [
    "정상",
    "기관사칭형",
    "대출사기형",
    "메신저피싱",
    "스미싱",
    "투자사기",
    "원격제어",
]
