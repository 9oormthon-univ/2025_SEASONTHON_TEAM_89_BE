import re
import json
import time
import asyncio
import datetime
from kiwipiepy import Kiwi

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.schemas.check_fraud import ChatResponse
from app.services.check_fraud_queue import CheckFraudQueue
from app.services.check_fraud_result_dict import CheckFraudResultDict

router = APIRouter()

# 형태소분석기 모델 로드
kiwi = Kiwi()
_ = kiwi.tokenize("모델 로드를 위한 워밍업 문장입니다.")

# 불완전한 단어 필터링용
is_incomplete_kor = re.compile(r"[ㄱ-ㅎ|ㅏ-ㅣ]")
# 특수문자 감지
detect_sf = re.compile(r'[\?\!\.]')

check_queue = CheckFraudQueue()
cfrd = CheckFraudResultDict()


@router.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    """
    # 핑 - 10초에 1회정도로 보내줘야함
    Request: {
        "type": "ping"
    }
    Response: {
        "type": "pong"
    }

    # 사기 메시지 체크
    Request: {
        "type": "check_fraud",
        "message": "체크할 메시지"
    }
    Response: {
        "result": {
            risk_level: "정상" or "주의" or "위험"
            confidence: 0.0 ~ 1.0
            detected_patterns: ["사기 패턴 리스트"]
            explanation: 사용자에게 제공할 간단한 설명
            recommended_action: "전송 전 확인" 같은게 들어감
        }
    }
    """
    # 웹소켓 오픈
    await websocket.accept()
    chat_data = ""
    try:
        while True:
            data = await websocket.receive_text()

            try:
                a = json.loads(data)
            except:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid JSON"}))
                continue
            if a.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            elif a.get("type") == "check_fraud":
                message = a.get("message")

                if len(message) <= 0:
                    await websocket.send_text("""{
                        "result": {
                            "risk_level": "정상",
                            "confidence": 1.0,
                            "detected_patterns": [],
                            "explanation": "빈 문장입니다.",
                            "recommended_action": ""
                        }
                    }""")
                    continue
                
                # 마지막 단어가 완벽한가? -> 형태소분석기로 문장 토크나이징 이전에
                # 완전히 작성된 문장의 기본 조건을 확인하기 위함
                if len(message) > 0 and is_incomplete_kor.match(message[-1]):
                    continue
                
                # 토크나이저의 EF 감지 정확도를 높이기 위해 종결 부호가 없는 문장에 . 삽입
                is_insert_sf = False
                # 토크나이저 공급용 변수
                tk_msg = message
                if len(message) > 0 and not detect_sf.match(message[-1]):
                    tk_msg += "."
                    is_insert_sf = True

                # 토크나이징
                token = kiwi.tokenize(tk_msg)

                # 마지막 토큰의 품사 태그가 EF(종결 어미) 혹은 SF(종결 부호)일 경우 완성된 문장으로 판단하여
                # LLM 판단 대기열에 추가

                if len(token) > 0:
                    # 마지막 토큰이 삽입한 종결 부호일 경우 제거
                    if is_insert_sf and token[-1].tag == "SF":
                        token = token[:-1]

                    if len(token) > 0 and token[-1].tag in ["EF", "SF"]:
                        await cfrd.create_event_for_message(message)

                        # 큐에 삽입
                        await check_queue.push(message)
                        # 응답 대기
                        res: ChatResponse = None

                        response_data = await cfrd.wait_for_result(message)

                        if response_data is not None:
                            res = ChatResponse(result=response_data)

                        if res:
                            await websocket.send_text(json.dumps(res.model_dump(), ensure_ascii=False))
                        else:
                            await websocket.send_text('{"type": "error", "message": "No response from LLM"}')

            else:
                await websocket.send_text('{"type": "error", "message": "Unknown message type"}')
    except WebSocketDisconnect:
        print("WebSocket disconnected")