
import threading

class CheckFraudResultDict:
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    # 인스턴스 변수들을 여기서 직접 초기화
                    cls._instance._result = {}
        return cls._instance
    
    def __init__(self):
        # __init__은 매번 호출될 수 있으므로 아무것도 하지 않음
        pass

    def insert(self, original_text: str, result_obj: dict):
        """
        요소 삽입
        """
        self._result[original_text] = result_obj

    def get(self, original_text: str):
        """
        큐에 있는 모든 요소 반환 및 제거
        """
        result = self._result.pop(original_text, None)
        return result