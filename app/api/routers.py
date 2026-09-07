from fastapi import APIRouter
from app.api.endpoints import auth, family_group, kakao_login, ml_data, ml_models, notifications

router = APIRouter()

router.include_router(family_group.router, prefix="/family_group", tags=["family_group"])
router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
router.include_router(kakao_login.router, prefix="/auth/kakao", tags=["kakao-login"])
router.include_router(auth.router, prefix="/auth", tags=["auth"])
router.include_router(ml_data.router, prefix="/ml", tags=["ml-data"])

router.include_router(ml_models.router, prefix="/ml", tags=["ml-models"])
