import uvicorn
import asyncio
import os
from contextlib import suppress
from fastapi import FastAPI
from contextlib import asynccontextmanager
from starlette.middleware.cors import CORSMiddleware

from app import WEB_HOST, WEB_PORT
from app.api import routers
from app.api.endpoints import legal
from app.services.check_fraud import start_processing
from app.services.model_training import training_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 시작 시 백그라운드 태스크 시작"""
    await start_processing()
    task = None
    if os.environ.get("ML_AUTO_TRAIN", "true").lower() in {"true", "1", "yes"}:
        task = asyncio.create_task(training_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

app = FastAPI(
    title="9oormthon Keyboard Backend",
    description="API Server",
    version="1.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(legal.router)
app.include_router(routers.router, prefix="/api")

if __name__ == "__main__":
    uvicorn.run(
        "app.__main__:app",
        host=WEB_HOST,
        port=WEB_PORT,
        reload=True
    )
