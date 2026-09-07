import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Union


_REPO_ROOT = Path(__file__).resolve().parents[2]
ML_INBOX_DIR = Path(
    os.environ.get("ML_INBOX_DIR", str(_REPO_ROOT / "data" / "ml_inbox"))
)
_UNSAFE_USER_ID_CHARACTER = re.compile(r"[^A-Za-z0-9_-]")
_UPLOAD_TIMESTAMP_PATTERN = r"\d{8}T\d{6}_\d{3}"


def safe_user_id(user_id: str) -> str:
    """Keep the historical filename format while removing path separators."""
    result = _UNSAFE_USER_ID_CHARACTER.sub("_", user_id)[:64]
    if not result:
        raise ValueError("user_id cannot produce an empty filename prefix")
    return result


def _resolved_inbox(inbox_dir: Union[str, Path]) -> Path:
    return Path(inbox_dir).expanduser().resolve()


def save_user_labeled_csv(
    user_id: str,
    body: bytes,
    inbox_dir: Union[str, Path] = ML_INBOX_DIR,
    timestamp: Optional[datetime] = None,
) -> Tuple[str, Path]:
    """Persist one upload beneath the configured inbox without path traversal."""
    inbox = _resolved_inbox(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)

    stamp = (timestamp or datetime.utcnow()).strftime("%Y%m%dT%H%M%S_%f")[:-3]
    filename = f"{safe_user_id(user_id)}_{stamp}.csv"
    filepath = (inbox / filename).resolve()
    if filepath.parent != inbox:
        raise ValueError("resolved upload path escaped ML_INBOX_DIR")

    # A timestamp collision must not overwrite an earlier user's training data.
    # Publish only complete CSVs. Hard-link creation preserves collision rejection.
    with tempfile.NamedTemporaryFile(dir=inbox, suffix=".pending", delete=False) as file:
        pending = Path(file.name)
        try:
            file.write(body)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
    try:
        os.link(pending, filepath)
    finally:
        pending.unlink(missing_ok=True)
    return filename, filepath


def delete_user_labeled_csv_files(
    user_id: str,
    inbox_dir: Union[str, Path] = ML_INBOX_DIR,
) -> int:
    """Delete only this user's canonical CSV uploads and return the count."""
    inbox = _resolved_inbox(inbox_dir)
    if not inbox.is_dir():
        return 0

    prefix = safe_user_id(user_id)
    expected_name = re.compile(
        rf"^{re.escape(prefix)}_{_UPLOAD_TIMESTAMP_PATTERN}\.csv$"
    )
    deleted = 0

    for candidate in inbox.iterdir():
        if not expected_name.fullmatch(candidate.name):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.resolve().parent != inbox:
            continue
        candidate.unlink()
        deleted += 1

    return deleted
