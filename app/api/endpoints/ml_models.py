"""Public, read-only model artifact routes; private training files are never served."""
import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from app.services.model_training import settings

router = APIRouter()


@router.get("/models/latest.json")
def latest_model():
    path = settings()["public"] / "latest.json"
    if not path.is_file():
        raise HTTPException(404, "No model has been released")
    return JSONResponse(json.loads(path.read_text()), headers={"Cache-Control": "no-store"})


@router.get("/models/releases/{version}/{name}")
def model_file(version: str, name: str):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}", version) or name not in {"phishing.onnx", "vocab.txt"}:
        raise HTTPException(404)
    root = settings()["public"]
    path = (root / "releases" / version / name).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})
