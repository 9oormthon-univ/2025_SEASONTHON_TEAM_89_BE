import asyncio

class CheckFraudResultDict:
    _instance = None
    _lock = asyncio.Lock() 

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data = {}
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, '_data'):
            self._data = {}

    async def create_event_for_message(self, message: str) -> None:
        """
        결과를 기다릴 메시지에 대해 Event 객체 생성하고 등록
        이 함수는 WebSocket 핸들러에서 호출됨
        """
        async with self._lock:
            if message not in self._data:
                event = asyncio.Event()
                self._data[message] = (event, None) # (이벤트 객체, 결과) 튜플 저장

    async def set_result_for_message(self, message: str, result: dict) -> None:
        """
        작업이 완료된 메시지에 대한 결과를 저장하고, 대기 중인 Event를 깨움
        이 함수는 LLM Worker에서 호출됨
        """
        async with self._lock:
            if message in self._data:
                event, _ = self._data[message]
                self._data[message] = (event, result)
                event.set() # Event를 설정하여 대기 중인 코루틴(wait_for_result)을 깨움

    async def wait_for_result(self, message: str, timeout: int = 10) -> dict | None:
        """
        메시지에 대한 결과가 설정될 때까지 비동기적으로 대기
        이 함수는 WebSocket 핸들러에서 호출됨
        """
        event_to_wait = None
        async with self._lock:
            if message in self._data:
                event_to_wait, _ = self._data[message]

        if event_to_wait is None:
            return None # 대기할 이벤트가 없음

        try:
            await asyncio.wait_for(event_to_wait.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"Timeout waiting for: {message}")
            async with self._lock:
                self._data.pop(message, None)
            return None
        
        # 결과 반환 및 데이터 정리
        async with self._lock:
            _, result = self._data.pop(message, (None, None))
            return result