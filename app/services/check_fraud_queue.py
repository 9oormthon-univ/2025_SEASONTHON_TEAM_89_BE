import asyncio

class CheckFraudQueue:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._queue = asyncio.Queue()
        return cls._instance

    def __init__(self):
        pass

    async def push(self, item):
        """
        큐에 요소를 비동기적으로 삽입
        """
        await self._queue.put(item)

    async def pop(self):
        """
        큐에서 요소를 비동기적으로 제거 및 반환
        큐가 비어있으면 아이템이 들어올 때까지 자동으로 대기
        """
        return await self._queue.get()