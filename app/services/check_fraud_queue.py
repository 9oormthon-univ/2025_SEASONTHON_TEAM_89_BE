
import threading

class CheckFraudQueue:
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    # 인스턴스 변수들을 여기서 직접 초기화
                    cls._instance._queue = []
        return cls._instance
    
    def __init__(self):
        # __init__은 매번 호출될 수 있으므로 아무것도 하지 않음
        pass

    def push(self, item):
        """
        큐에 요소 삽입
        """
        self._queue.append(item)

    def get_all(self):
        """
        큐에 있는 모든 요소 반환
        """
        return self._queue.copy()  # 원본 큐를 보호하기 위해 복사본 반환
    
    def pop(self):
        """
        큐에서 가장 오래된 요소 제거 및 반환
        """
        if self._queue:
            item = self._queue.pop(0)
            return item
        else:
            return None