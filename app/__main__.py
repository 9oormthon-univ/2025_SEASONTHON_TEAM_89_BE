import uvicorn
import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager
from starlette.middleware.cors import CORSMiddleware

from app import WEB_HOST, WEB_PORT
from app.api import routers
from app.services.check_fraud import start_processing


@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 시작 시 백그라운드 태스크 시작"""
    await start_processing()
    yield

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

app.include_router(routers.router, prefix="/api")

if __name__ == "__main__":
    uvicorn.run(
        "app.__main__:app",
        host=WEB_HOST,
        port=WEB_PORT,
        reload=True
    )