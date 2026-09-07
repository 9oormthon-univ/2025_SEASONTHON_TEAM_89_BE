from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse


router = APIRouter(include_in_schema=False)
_LEGAL_DIR = Path(__file__).resolve().parents[2] / "legal"
_PUBLIC_HEADERS = {
    "Cache-Control": "public, max-age=300",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/privacy-policy.html")
def privacy_policy() -> FileResponse:
    return FileResponse(
        _LEGAL_DIR / "privacy-policy.html",
        media_type="text/html; charset=utf-8",
        headers=_PUBLIC_HEADERS,
    )


@router.get("/account-deletion.html")
def account_deletion() -> FileResponse:
    return FileResponse(
        _LEGAL_DIR / "account-deletion.html",
        media_type="text/html; charset=utf-8",
        headers=_PUBLIC_HEADERS,
    )
