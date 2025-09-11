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
        prompt = f"""You are an AI expert specializing in detecting financial fraud, investment scams, and phishing within Korean messaging conversations. Your purpose is to analyze conversational context and identify genuine patterns of manipulation and deception. Be accurate and balanced - do not over-classify normal conversations as suspicious. AND PLEASE think step by step before concluding your analysis.  

            
CRITICAL: Analyze ONLY the message provided in the ANALYSIS SECTION below. Do NOT confuse it with the examples.

Your output MUST be a single, valid JSON object and nothing else. Do not include any explanatory text, code blocks, or markdown formatting before or after the JSON.

IMPORTANT GUIDELINES:
Normal daily conversations (games, casual chat, greetings, food questions) should be marked as "정상" with high confidence
Only mark as "주의" or "위험" when there are clear fraud indicators
Consider context and common sense - not every mention of money or urgency is a scam
Be precise with confidence scores based on actual evidence

The JSON object must conform to the following schema:
{{
  "risk_level": "string", // Must be one of: "정상", "주의", "위험"
  "confidence": "float", // A value between 0.0 and 1.0 indicating the confidence of the risk_level assessment.
  "detected_patterns": "array[string]", // A list of detected scam patterns. Examples: "과도한 수익 보장", "긴급한 입금 요구", "개인정보 요구", "비공개 정보 언급", "의심스러운 링크"
  "explanation": "string", // A brief, clear explanation in Korean for the user (max 50 characters).
  "recommended_action": "string" // Must be one of: "전송 전 확인", "전송 중단 권고", "없음"
}}
===== TRAINING EXAMPLES (DO NOT ANALYZE THESE) =====

Example A - ACTUAL SCAM (주의):
Input: "혹시 말씀해주신 계좌로 새 상품 재주문하고 기존 제품에 대한 비용을 환불해주신다는 거죠?"
Output: {{"risk_level": "주의", "confidence": 0.75, "detected_patterns": ["환불 요구"], "explanation": "환불을 요구할 경우 사기일 가능성이 있어 주의해야 합니다.", "recommended_action": "전송 중단 권고"}}

Example B - NORMAL FOOD QUESTION (정상):
Input: "내일 학식 뭐야?"
Output: {{"risk_level": "정상", "confidence": 0.99, "detected_patterns": [], "explanation": "학교 급식에 대한 일상적인 질문입니다.", "recommended_action": "없음"}}

Example C - NORMAL GAME (정상):
Input: "롤이나 하자"
Output: {{"risk_level": "정상", "confidence": 0.99, "detected_patterns": [], "explanation": "게임 제안으로 정상적인 대화입니다.", "recommended_action": "없음"}}

Example D - INVESTMENT SCAM (위험):
Input: "수익률이 200% 라구요?"
Output: {{"risk_level": "위험", "confidence": 0.98, "detected_patterns": ["과도한 수익 보장"], "explanation": "비현실적인 수익률을 제안하는 사기 수법에 노출된 상태일 가능성이 높습니다.", "recommended_action": "전송 중단 권고"}}

Example E - NORMAL MESSENGER (주의):
Input: "무슨 부탁인데?"
Output: {{"risk_level": "주의", "confidence": 0.75, "detected_patterns": [""], "explanation": "일상적인 대화지만 갑작스런 금전 부탁인 경우 주의가 필요합니다.", "recommended_action": "없음"}}

Example F - MONEY REQUEST (주의):
Input: "얼마 보내면 돼요?"
Output: {{"risk_level": "주의", "confidence": 0.75, "detected_patterns": ["직접적인 금전 요구"], "explanation": "직접적인 금전 요구는 사기일 가능성이 높으나 일상 대화일수도 있음.", "recommended_action": "전송 중단 권고"}}

Example G - PERSONAL INFO (위험):
Input: "카드 정보/인증서만 입력하면 되죠?"
Output: {{"risk_level": "위험", "confidence": 0.90, "detected_patterns": ["개인 금융 정보 요구"], "explanation": "금융 정보 입력 요구는 매우 위험합니다.", "recommended_action": "전송 중단 권고"}}

Example H - PSYCHOLOGICAL PRESSURE (주의):
Input: "이거 진짜 맞는 거지?"
Output: {{"risk_level": "주의", "confidence": 0.83, "detected_patterns": ["심리적 압박"], "explanation": "의심이 든다면 즉시 대화를 중단하세요.", "recommended_action": "전송 중단 권고"}}

Example I - TRANSFER URGENCY (주의):
Input: "지금 바로 이체하라고?"
Output: {{"risk_level": "위험", "confidence": 0.92, "detected_patterns": ["송금 재촉"], "explanation": "급한 송금 요구는 사기의 전형적인 수법입니다.", "recommended_action": "전송 중단 권고"}}

Example I - TRANSFER MONEY (위험):
Input: "너만 믿고 넣는다"
Output: {{"risk_level": "위험", "confidence": 0.85, "detected_patterns": ["과도한 신용"], "explanation": "상대방에 대한 과도한 신뢰는 사기의 위험 요소입니다.", "recommended_action": "전송 중단 권고"}}

Example J - ACCOUNT ABUSE (위험):
Input: "대포통장"
Output: {{"risk_level": "위험", "confidence": 0.99, "detected_patterns": ["대포통장 언급"], "explanation": "대포통장은 불법 금융거래에 사용됩니다.", "recommended_action": "전송 중단 권고"}}

Example K - PERSONAL INFO LEAK (위험):
Input: "개인정보유출"
Output: {{"risk_level": "위험", "confidence": 0.95, "detected_patterns": ["개인정보 유출"], "explanation": "개인정보 유출은 매우 심각한 보안 위험입니다.", "recommended_action": "전송 중단 권고"}}

Example L - STRANGER CONTACT (주의):
Input: "모르는 사람"
Output: {{"risk_level": "주의", "confidence": 0.70, "detected_patterns": ["신원 미확인"], "explanation": "모르는 사람과의 거래는 주의가 필요합니다.", "recommended_action": "전송 전 확인"}}

Example M - LOAN OFFER (주의):
Input: "대출이 가능한거에요?"
Output: {{"risk_level": "주의", "confidence": 0.80, "detected_patterns": ["대출 제안"], "explanation": "대출 제안은 사기일 가능성을 확인해야 합니다.", "recommended_action": "전송 전 확인"}}

Example N - SUSPICIOUS LINK (위험):
Input: "링크에 들어가라고요?"
Output: {{"risk_level": "위험", "confidence": 0.90, "detected_patterns": ["의심스러운 링크"], "explanation": "모르는 링크 접속 요구는 피싱 시도일 수 있습니다.", "recommended_action": "전송 중단 권고"}}

Example O - ACCOUNT ABUSE (위험):
Input: "대리결제"
Output: {{"risk_level": "위험", "confidence": 0.99, "detected_patterns": ["대리결제 언급"], "explanation": "대리결제는 사기 가능성이 있습니다.", "recommended_action": "전송 중단 권고"}}

Example P - ILLEGAL LEADING GROUP (위험):
Input: "오늘 리딩 너무 좋네요"
Output: {{"risk_level": "위험", "confidence": 0.95, "detected_patterns": ["불법 리딩방 경고"], "explanation": "불법 리딩방이 의심됩니다", "recommended_action": "전송 중단 권고"}}

Example Q - ILLEGAL LEADING GROUP (위험):
Input: "믿음은 곧 수익입니다."
Output: {{"risk_level": "위험", "confidence": 0.83, "detected_patterns": ["불법 리딩방 경고", "과도한 믿음"], "explanation": "불법 리딩방이 의심됩니다", "recommended_action": "전송 중단 권고"}}

Example R - ILLEGAL LEADING GROUP (위험):
Input: "님만 믿습니다!! 가즈아~!!!"
Output: {{"risk_level": "주의", "confidence": 0.73, "detected_patterns": ["불법 리딩방 주의", "과도한 믿음"], "explanation": "불법 리딩방이 의심됩니다", "recommended_action": "전송 중단 권고"}}

===== END OF EXAMPLES =====

===== ACTUAL ANALYSIS TASK =====

IMPORTANT: Analyze ONLY this message below. Ignore all examples above.

Current Message to Analyze:
"{original_text}"

Analyze the above message and provide accurate JSON output based on its actual content.
"""
        data = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False
        }

        response = await client.post(f"{OLLAMA_URL}/api/generate", json=data, timeout=None)
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

                cfrd.insert(original_text, result_LLMResponse)
            except Exception as e:
                print(f"[ERROR] 큐 처리 중 오류 발생: {e}")
                # 자세한 오류 출력
                # import traceback
                # traceback.print_exc()
                # handle error (e.g., log or requeue)
                pass
        else:
            await asyncio.sleep(0.1)  # wait before checking again

async def start_processing():
    """백그라운드 큐 처리 태스크 시작"""
    task = asyncio.create_task(process_queue(CheckFraudQueue(), CheckFraudResultDict()))
    return task