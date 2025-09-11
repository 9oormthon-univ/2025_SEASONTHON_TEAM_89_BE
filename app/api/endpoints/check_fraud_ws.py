import json
import asyncio
import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app.schemas.check_fraud import ChatRequest, ChatResponse
from app.services.check_fraud_queue import CheckFraudQueue
from app.services.check_fraud_result_dict import CheckFraudResultDict

router = APIRouter()


@router.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

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

                # 큐에 삽입
                CheckFraudQueue().push(message)
                # 응답 대기
                start_time = datetime.datetime.now()
                
                res: ChatResponse = None
                
                # 0.1 초마다 확인, 최대 10초 대기
                while (datetime.datetime.now() - start_time).seconds < 10:
                    response = CheckFraudResultDict().get(message)
                    if response is False:
                        response = None
                        break
                    if response is not None:
                        break
                    await asyncio.sleep(0.1)
                
                res = ChatResponse(result=response)

                await websocket.send_text(json.dumps(res.model_dump()))
            else:
                await websocket.send_text(json.dumps({"type": "error", "message": "Unknown message type"}))
    except WebSocketDisconnect:
        print("WebSocket disconnected")