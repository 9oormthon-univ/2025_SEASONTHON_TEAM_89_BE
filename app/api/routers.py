from fastapi import APIRouter
from app.api.endpoints import check_fraud, family_group, check_fraud_ws, kakao_login, notifications

router = APIRouter()

router.include_router(check_fraud.router, prefix="/check_fraud", tags=["check_fraud"])
router.include_router(family_group.router, prefix="/family_group", tags=["family_group"])
router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
router.include_router(check_fraud_ws.router, prefix="/ws/fraud", tags=["fraud-websocket"])
router.include_router(kakao_login.router, prefix="/auth/kakao", tags=["kakao-login"])