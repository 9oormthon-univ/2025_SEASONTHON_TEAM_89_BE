from fastapi import APIRouter
from app.api.endpoints import check_fraud, family_group, check_fraud_ws

router = APIRouter()

router.include_router(check_fraud.router, prefix="/check_fraud", tags=["check_fraud"])
router.include_router(family_group.router, prefix="/family_group", tags=["family_group"])
router.include_router(check_fraud_ws.router, prefix="/ws", tags=["websocket"])