from fastapi import APIRouter, HTTPException, Depends, status, Request
import logging

from app.api.dependencies import enforce_actor, require_current_user
from app.services.ml_data_storage import save_user_labeled_csv

logger = logging.getLogger(__name__)
router = APIRouter()

# 라벨링 CSV 고정 헤더 — 안드로이드 클라와 합의한 스펙(순서/컬럼 불일치 시 422)
EXPECTED_HEADER = "text,label,engine_verdict,patterns,score,source,app,timestamp"

# 원시 바디 최대 크기(5MB) — 초과 시 413
MAX_CSV_BYTES = 5 * 1024 * 1024

@router.post(
    "/labeled-csv",
    status_code=status.HTTP_201_CREATED,
    summary="라벨링 CSV 업로드 (user_id 기반)",
    description="클라 감지 이력 라벨링 CSV(원시 바디)를 그대로 수신해 학습 데이터로 서버에 저장한다. "
                "본문은 UTF-8(BOM) text/csv, 헤더 한 줄이 스펙과 다르면 422로 거부한다.",
)
async def upload_labeled_csv(
    request: Request,
    user_id: str,
    current_user=Depends(require_current_user),
):
    """
    라벨링 CSV 업로드 API

    - user_id: 사용자 ID (쿼리 파라미터)
    - body: 원시 CSV 바이트 (UTF-8 with BOM, Content-Type: text/csv; charset=utf-8)

    auth/device-token 과 동일하게 user_id 기반으로 사용자 존재/활성 여부를 검증한다.
    """
    # 1) JWT actor와 호환용 user_id 쿼리 필드가 같은지 검증
    enforce_actor(current_user, user_id)

    # 2) 원시 바디 수신 (python-multipart 미설치 — UploadFile/File 사용 금지)
    body = await request.body()

    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="빈 본문",
        )

    # 3) 5MB 초과 거부
    if len(body) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="CSV 크기 초과(최대 5MB)",
        )

    # 4) UTF-8(BOM) 디코딩 — 학습 임포터가 utf-8-sig 로 읽으므로 저장 시 BOM 은 보존한다.
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="UTF-8 아님",
        )

    # 5) 고정 헤더 검증 — 첫 줄이 스펙과 다르면 422
    first_line = text.split("\n", 1)[0].rstrip("\r")
    if first_line != EXPECTED_HEADER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CSV 헤더 불일치",
        )

    # 6) 저장 — ML_INBOX_DIR 하위의 충돌 안전 파일로 원시 바이트를 기록
    try:
        filename, _ = save_user_labeled_csv(user_id, body)
    except Exception as e:
        logger.error(f"라벨링 CSV 저장 실패: user_id={user_id}, error={str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CSV 저장 실패",
        )

    logger.info(f"라벨링 CSV 저장(POST /ml/labeled-csv): user_id={user_id}, file={filename}, bytes={len(body)}")
    return {
        "success": True,
        "filename": filename,
        "bytes": len(body),
    }
