import asyncio
from datetime import datetime
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


def test_existing_backend_routes_serve_only_model_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("ML_MODEL_PUBLIC_DIR", str(tmp_path))
    from app.api.endpoints.ml_models import router
    app = FastAPI()
    app.include_router(router, prefix="/api/ml")
    client = TestClient(app)
    assert client.get("/api/ml/models/latest.json").status_code == 404
    manifest = {"version": "v1"}
    (tmp_path / "latest.json").write_text(json.dumps(manifest))
    artifact = tmp_path / "releases/v1"
    artifact.mkdir(parents=True)
    (artifact / "phishing.onnx").write_bytes(b"model")
    (artifact / "private.csv").write_text("not public")
    assert client.get("/api/ml/models/latest.json").json() == manifest
    assert client.get("/api/ml/models/latest.json").headers["cache-control"] == "no-store"
    assert client.get("/api/ml/models/releases/v1/phishing.onnx").content == b"model"
    assert client.get("/api/ml/models/releases/v1/private.csv").status_code == 404
    outside = tmp_path.parent / "outside.onnx"
    outside.write_bytes(b"private")
    (artifact / "vocab.txt").symlink_to(outside)
    assert client.get("/api/ml/models/releases/v1/vocab.txt").status_code == 404


def test_upload_is_atomic_and_collision_keeps_original(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    from app.services.ml_data_storage import save_user_labeled_csv
    timestamp = datetime(2026, 9, 8, 1, 2, 3)
    name, path = save_user_labeled_csv("u1", b"first", tmp_path, timestamp)
    with pytest.raises(FileExistsError):
        save_user_labeled_csv("u1", b"second", tmp_path, timestamp)
    assert path.read_bytes() == b"first"
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]


def test_worker_reuses_configured_inbox_and_runs_as_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    from app.services import model_training
    monkeypatch.setattr(model_training, "ML_INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setenv("ML_PYTHON", "/test/python")
    monkeypatch.setenv("ML_MODEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ML_BASE_CHECKPOINT", str(tmp_path / "missing"))
    command = model_training.command()
    assert command[0] == "/test/python"
    assert command[command.index("--inbox") + 1] == str(tmp_path / "inbox")
    assert not model_training.ready()

    async def cancel_waiting_worker():
        task = asyncio.create_task(model_training.training_loop())
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel_waiting_worker())
