from fastapi import APIRouter, HTTPException, Depends, status, Request
from sqlalchemy.orm import Session
from datetime import datetime
import os
import re
import logging

from app.core.database import get_db
from app.repositories.user_repository import get_user_repository

logger = logging.getLogger(__name__)
router = APIRouter()

# 라벨링 CSV 고정 헤더 — 안드로이드 클라와 합의한 스펙(순서/컬럼 불일치 시 422)
EXPECTED_HEADER = "text,label,engine_verdict,patterns,score,source,app,timestamp"

# 원시 바디 최대 크기(5MB) — 초과 시 413
MAX_CSV_BYTES = 5 * 1024 * 1024

# 저장 디렉터리 — 없으면 <repo>/data/ml_inbox 로 기본 설정.
# ml/phishing-classifier 학습 파이프라인의 import_csv.py 가 data/inbox/*.csv 를 glob 하므로,
# 서버 운영자는 ML_INBOX_DIR 를 그 inbox 로 지정하거나 이 디렉터리를 rsync 한다.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ML_INBOX_DIR = os.environ.get("ML_INBOX_DIR", os.path.join(_REPO_ROOT, "data", "ml_inbox"))


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
    db: Session = Depends(get_db),
):
    """
    라벨링 CSV 업로드 API

    - user_id: 사용자 ID (쿼리 파라미터)
    - body: 원시 CSV 바이트 (UTF-8 with BOM, Content-Type: text/csv; charset=utf-8)

    auth/device-token 과 동일하게 user_id 기반으로 사용자 존재/활성 여부를 검증한다.
    """
    # 1) 사용자 검증 — 없거나 비활성이면 404
    user_repo = get_user_repository(db)
    user = user_repo.get_by_user_id(user_id)

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없음",
        )

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

    # 6) 저장 — 디렉터리 보장 후, 충돌 안전 파일명으로 원시 바이트(BOM 포함) 그대로 기록
    os.makedirs(ML_INBOX_DIR, exist_ok=True)

    # 경로 조작 방지: user_id 를 안전 문자로 정규화(최대 64자)
    safe_user_id = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:64]
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S_%f")[:-3]  # ms 정밀도
    filename = f"{safe_user_id}_{stamp}.csv"
    filepath = os.path.join(ML_INBOX_DIR, filename)

    try:
        with open(filepath, "wb") as f:
            f.write(body)
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
