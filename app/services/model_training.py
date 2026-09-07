"""Run durable ML inbox checks without importing torch in API processes."""
import asyncio
import logging
import os
import signal
from pathlib import Path
import sys

from app.services.ml_data_storage import ML_INBOX_DIR

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger(__name__)


def settings():
    def path(key, fallback):
        return Path(os.environ.get(key, str(ROOT / fallback))).expanduser().resolve()
    return {
        "inbox": Path(ML_INBOX_DIR).resolve(),
        "state": path("ML_MODEL_STATE_DIR", "data/ml_state"),
        "public": path("ML_MODEL_PUBLIC_DIR", "data/ml_public"),
        "data": path("ML_TRAIN_DATA_DIR", "data/ml_assets/data"),
        "base": path("ML_BASE_CHECKPOINT", "data/ml_assets/checkpoint"),
        "champion": path("ML_CHAMPION_DIR", "data/ml_assets/champion"),
    }


def command():
    args = [os.environ.get("ML_PYTHON", sys.executable), str(ROOT / "ml/phishing-classifier/auto_train.py")]
    for key, value in settings().items():
        args.extend(["--" + key, str(value)])
    args.extend(["--min-new", os.environ.get("ML_MIN_NEW_LABELS", "20"),
                 "--cooldown", os.environ.get("ML_TRAIN_COOLDOWN_SECONDS", "21600")])
    return args


def ready():
    paths = settings()
    return all((paths["data"] / f"{name}.jsonl").is_file() for name in ("train", "val", "test", "golden")) and all(
        path.is_file() for path in (paths["base"] / "config.json", paths["base"] / "model.safetensors",
                                    paths["champion"] / "phishing.onnx", paths["champion"] / "vocab.txt"))


async def training_loop():
    paths = settings()
    paths["inbox"].mkdir(parents=True, exist_ok=True)
    paths["state"].mkdir(parents=True, exist_ok=True)
    process = None
    try:
        while True:
            if ready():
                with (paths["state"] / "worker.log").open("ab") as output:
                    process = await asyncio.create_subprocess_exec(*command(), stdout=output, stderr=output, start_new_session=True)
                    result = await process.wait()
                    if result:
                        LOG.warning("ML training check failed (exit %s); see worker.log", result)
                    process = None
            else:
                LOG.info("ML assets/data not configured; see docs/model-deployment.md")
            await asyncio.sleep(60)
    finally:
        if process is not None and process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
