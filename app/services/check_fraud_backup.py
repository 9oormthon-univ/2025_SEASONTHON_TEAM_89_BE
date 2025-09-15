import re
import json
import httpx
import asyncio

from .check_fraud_queue import CheckFraudQueue
from .check_fraud_result_dict import CheckFraudResultDict
from app.schemas.check_fraud import LLMResponse

from app import OLLAMA_URL, OLLAMA_MODEL

find_res = re.compile(r'({\n?\s*"risk_level":\s?"(정상|주의|위험)",\n?\s*"confidence":\s?((\d|\.)+),\n?\s+"detected_patterns":\s?(\[.*\]),\n?\s*"explanation":\s?"(.*)",\n?\s*"recommended_action":\s?"(.*)"\n?})')

async def request_ollama(original_text: str):
    async with httpx.AsyncClient() as client:
        prompt = f"""===== ACTUAL ANALYSIS TASK =====

IMPORTANT: Analyze ONLY this message below. Ignore all examples above.

Current Message to Analyze:
"{original_text}"

Analyze the above message and provide accurate JSON output based on its actual content.
"""
        data = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }

        response = await client.post(f"{OLLAMA_URL}/api/generate", json=data, timeout=None)
        print(response.json()['response'])
        return response.json()['response'].replace('\"', '"')

async def process_queue(cfq: CheckFraudQueue, cfrd: CheckFraudResultDict):
    while True:
        original_text = cfq.pop()
        if original_text is not None:
            try:
                status = "failed"
                res = None
                result_LLMResponse = None
                for _ in range(3):  # Retry up to 3 times
                    result = await request_ollama(original_text)
                    res = find_res.findall(result)

                    if res:
                        status = "success"
                        break
                    await asyncio.sleep(0.1)

                if status == "success":
                    result_dict = json.loads(res[0][0])
                    result_LLMResponse = LLMResponse(**result_dict)
                else:
                    result_LLMResponse = False

                # cfrd.insert(original_text, result_LLMResponse)
                await cfrd.set_result_for_message(original_text, result_LLMResponse)
            except Exception as e:
                print(f"[ERROR] 큐 처리 중 오류 발생: {e}")
                # 자세한 오류 출력
                # import traceback
                # traceback.print_exc()
                # handle error (e.g., log or requeue)
                pass
        else:
            await asyncio.sleep(0.01)  # wait before checking again

async def start_processing():
    """백그라운드 큐 처리 태스크 시작"""
    task = asyncio.create_task(process_queue(CheckFraudQueue(), CheckFraudResultDict()))
    return task