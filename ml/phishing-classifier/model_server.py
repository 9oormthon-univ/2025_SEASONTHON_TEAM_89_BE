"""Read-only release API. Run behind the existing HTTPS reverse proxy."""
from __future__ import annotations

import os
import json
from pathlib import Path
import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse


def create_app(public: Path | None = None) -> FastAPI:
    public = (public or Path(os.environ["ML_MODEL_PUBLIC_DIR"])).resolve()
    app = FastAPI(title="Weheome classifier releases", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/ml/models/latest.json")
    def latest():
        path = public / "latest.json"
        if not path.is_file():
            raise HTTPException(404, "No released model")
        return JSONResponse(json.loads(path.read_text()), headers={"Cache-Control": "no-store"})

    @app.get("/api/ml/models/releases/{version}/{name}")
    def artifact(version: str, name: str):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}", version) or name not in {"phishing.onnx", "vocab.txt"}:
            raise HTTPException(404)
        path = (public / "releases" / version / name).resolve()
        if public not in path.parents or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    return app
