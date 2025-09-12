from pydantic import BaseModel

class ChatRequest(BaseModel):
    message: str

class LLMResponse(BaseModel):
    risk_level: str  # "정상", "주의", "위험"
    confidence: float  # 0.0 ~ 1.0
    detected_patterns: list[str]  # 사기 패턴 리스트
    explanation: str  # 사용자에게 제공할 간단한 설명 (최대 50자)
    recommended_action: str  # "전송 전 확인", "전송 중단 권고", "없음"

class ChatResponse(BaseModel):
    result: LLMResponse | None = None