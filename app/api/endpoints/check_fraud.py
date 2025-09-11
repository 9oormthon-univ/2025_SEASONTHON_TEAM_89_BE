import asyncio
import datetime
from fastapi import APIRouter

from app.schemas.check_fraud import ChatRequest, ChatResponse
from app.services.check_fraud_queue import CheckFraudQueue
from app.services.check_fraud_result_dict import CheckFraudResultDict

router = APIRouter()


@router.post(
    "/",
    response_model=ChatResponse,
    summary="메시지 사기 탐지",
    description="""
    ```
        Request:
            message: 메시지

        Response:
            result: {
                risk_level: "정상" or "주의" or "위험"
                confidence: 0.0 ~ 1.0
                detected_patterns: ["사기 패턴 리스트"]
                explanation: 사용자에게 제공할 간단한 설명
                recommended_action: "전송 전 확인" 같은게 들어감
            } | None
        
        실패했을 경우:
            result: null
    """
)
async def check_fraud(data: ChatRequest):
    # 큐에 삽입
    CheckFraudQueue().push(data.message)
    # 응답 대기
    start_time = datetime.datetime.now()
    
    res: ChatResponse = None
    
    # 0.01 초마다 확인, 최대 20초 대기
    while (datetime.datetime.now() - start_time).seconds < 20:
        response = CheckFraudResultDict().get(data.message)
        if response is False:
            response = None
            break
        if response is not None:
            break
        await asyncio.sleep(0.01)
    
    res = ChatResponse(result=response)
    
    return res