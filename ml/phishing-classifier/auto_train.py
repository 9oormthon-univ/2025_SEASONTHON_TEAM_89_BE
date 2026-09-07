#!/usr/bin/env python3
"""Durable inbox -> real training -> exported ONNX evaluation -> atomic publication.

Run from a systemd timer. Uses the existing authenticated backend's ML_INBOX_DIR;
does not create a second unauthenticated CSV upload endpoint.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from labels import RISK_LABELS
from pii import scrub
from mobile_eval import evaluate

HERE = Path(__file__).resolve().parent
LOG = logging.getLogger("auto_train")
MAX_CSV_BYTES = 5 * 1024 * 1024


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(source.read()).hexdigest()


def rows_at(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def clean_rows(rows: list[dict]) -> list[dict]:
    result = {}
    for row in rows:
        text = scrub(str(row.get("text", "")).strip())
        if len(text) >= 2 and row.get("label") in RISK_LABELS:
            result[text] = {"text": text, "label": row["label"], "origin": row.get("origin", "unknown")}
    return list(result.values())


def read_feedback(inbox: Path) -> list[dict]:
    labels: dict[str, set[str]] = {}
    for path in sorted(inbox.glob("*.csv")):
        # Avoid files still being written by the backend; the next timer will pick them up.
        if path.is_symlink() or time.time() - path.stat().st_mtime < 5:
            continue
        try:
            with path.open("rb") as source:
                data = source.read(MAX_CSV_BYTES + 1)
            if len(data) > MAX_CSV_BYTES:
                raise ValueError("CSV exceeds limit")
            reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
            if not {"text", "label", "engine_verdict"} <= set(reader.fieldnames or []):
                raise ValueError("CSV header mismatch")
            for row in reader:
                text = scrub((row.get("text") or "").strip())
                label = (row.get("label") or "").strip()
                # No pseudo-labels: engine_verdict is never a training target.
                if 2 <= len(text) <= 1000 and label in RISK_LABELS:
                    labels.setdefault(text, set()).add(label)
        except (OSError, UnicodeError, csv.Error, ValueError):
            LOG.warning("Skipping invalid inbox file: %s", path.name)
    # Conflicting feedback is held out rather than arbitrarily choosing one person's label.
    return [{"text": text, "label": next(iter(values)), "origin": "user_confirmed"}
            for text, values in sorted(labels.items()) if len(values) == 1]


def fingerprint(row: dict) -> str:
    return hashlib.sha256((row["text"] + "\0" + row["label"]).encode()).hexdigest()


def prepare_run(data: Path, feedback: list[dict], target: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    val = clean_rows(rows_at(data / "val.jsonl"))
    suites = {p.stem: clean_rows(rows_at(p)) for p in sorted(data.glob("bench_*.jsonl"))}
    for name in ("test", "golden"):
        suites[name] = clean_rows(rows_at(data / f"{name}.jsonl"))
    if not all(suites.values()):
        raise ValueError("Evaluation suites must not be empty")
    evaluation_texts = {r["text"] for rows in suites.values() for r in rows}
    val = [row for row in val if row["text"] not in evaluation_texts]
    held = evaluation_texts | {r["text"] for r in val}
    eligible = [r for r in feedback if r["text"] not in held]
    # Validated base corpus prevents training a model on only risky reports.
    train = {r["text"]: r for r in clean_rows(rows_at(data / "train.jsonl")) if r["text"] not in held}
    train.update({r["text"]: r for r in eligible})
    for split in (list(train.values()), val):
        if set(r["label"] for r in split) != set(RISK_LABELS):
            raise ValueError("Training and validation both need all three classes")
    target.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train", list(train.values())), ("val", val)):
        (target / f"{name}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return eligible, suites


def gate(candidate: dict, champion: dict, tolerance: float = .01) -> list[str]:
    failures = []
    for suite, baseline in champion.items():
        new = candidate[suite]
        for metric in ("macro_f1", "danger_recall"):
            if metric in baseline and new.get(metric, -1) + tolerance < baseline[metric]:
                failures.append(f"{suite}: {metric} regressed")
        if "normal_fp" in baseline and new.get("normal_fp", 1) > baseline["normal_fp"] + tolerance:
            failures.append(f"{suite}: normal_fp regressed")
    return failures


def publish(public: Path, model: Path, vocab: Path, report: dict, version: str | None = None) -> dict:
    version = version or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    if not version or len(version) > 80 or not version[0].isalnum() or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in version):
        raise ValueError("Invalid version")
    releases = public / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    stage = releases / (".pending-" + uuid.uuid4().hex)
    stage.mkdir()
    try:
        shutil.copyfile(model, stage / "phishing.onnx")
        shutil.copyfile(vocab, stage / "vocab.txt")
        files = {name: {"size": (stage / name).stat().st_size, "sha256": digest(stage / name)}
                 for name in ("phishing.onnx", "vocab.txt")}
        manifest = {"schema_version": 1, "version": version, "min_app_version": 17,
                    "labels": list(RISK_LABELS), "tokenizer": "bert-wordpiece-cased-v1",
                    "max_length": 128, "files": files}
        atomic_json(stage / "manifest.json", manifest)
        # Metrics contain counts/scores only. Never publish CSVs, texts or checkpoints.
        atomic_json(stage / "metrics.json", report)
        if (releases / version).exists():
            raise ValueError("Release versions are immutable")
        stage.rename(releases / version)
        atomic_json(public / "latest.json", manifest)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def command(args: list[str], log: Path) -> None:
    with log.open("a") as output:
        subprocess.run(args, cwd=HERE, stdout=output, stderr=subprocess.STDOUT, check=True, timeout=6 * 3600)


def cycle(args) -> str:
    args.state.mkdir(parents=True, exist_ok=True)
    with (args.state / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "already_running"
        state_file = args.state / "state.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        feedback = read_feedback(args.inbox)
        work = args.state / "runs" / uuid.uuid4().hex
        eligible, suites = prepare_run(args.data, feedback, work / "data")
        keys = sorted(fingerprint(row) for row in eligible)
        new = set(keys) - set(state.get("trained_keys", []))
        if len(new) < args.min_new or (not args.force and time.time() - state.get("last_attempt", 0) < args.cooldown):
            shutil.rmtree(work)
            return "waiting_for_feedback"
        state["last_attempt"] = time.time()
        atomic_json(state_file, state)
        atomic_json(args.state / "status.json", {"status": "training", "run": work.name, "new_labels": len(new)})
        try:
            base = state.get("checkpoint") or str(args.base)
            command([sys.executable, str(HERE / "train.py"), "--base", base,
                     "--data-dir", str(work / "data"), "--out", str(work / "checkpoint"),
                     "--epochs", str(args.epochs), "--batch", str(args.batch), "--loss", "weighted"],
                    work / "train.log")
            checkpoint = work / "checkpoint"
            config = json.loads((checkpoint / "config.json").read_text())
            if config["id2label"] != {str(i): label for i, label in enumerate(RISK_LABELS)}:
                raise ValueError("Model label order mismatch")
            tok_config = json.loads((checkpoint / "tokenizer_config.json").read_text())
            if tok_config.get("do_lower_case", False):
                raise ValueError("Android requires a cased WordPiece tokenizer")
            command([sys.executable, str(HERE / "export.py"), "--model", str(checkpoint),
                     "--out", str(work / "export")], work / "train.log")
            model, vocab = work / "export/model.int8.onnx", checkpoint / "vocab.txt"
            # This evaluates the actual INT8 artifact, not just the pre-export HF model.
            candidate = {name: evaluate(model, vocab, rows) for name, rows in suites.items()}
            latest = args.public / "latest.json"
            if latest.exists():
                version = json.loads(latest.read_text())["version"]
                current = args.public / "releases" / version
                champion_model, champion_vocab = current / "phishing.onnx", current / "vocab.txt"
            else:
                champion_model, champion_vocab = args.champion / "phishing.onnx", args.champion / "vocab.txt"
            champion = {name: evaluate(champion_model, champion_vocab, rows) for name, rows in suites.items()}
            failures = gate(candidate, champion, args.tolerance)
            report = {"candidate": candidate, "champion": champion, "failures": failures}
            atomic_json(work / "evaluation.json", report)
            if failures:
                # Do not spend GPU time on the exact same rejected corpus every timer tick.
                state["trained_keys"] = keys
                atomic_json(state_file, state)
                atomic_json(args.state / "status.json", {"status": "rejected", "run": work.name, "reasons": failures})
                return "rejected"
            manifest = publish(args.public, model, vocab, report)
            state.update(trained_keys=keys, checkpoint=str(checkpoint), version=manifest["version"])
            atomic_json(state_file, state)
            atomic_json(args.state / "status.json", {"status": "published", "version": manifest["version"]})
            return "published"
        except Exception as error:
            atomic_json(args.state / "status.json", {"status": "failed", "run": work.name, "error": type(error).__name__})
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=HERE / "data")
    parser.add_argument("--base", type=Path, default=HERE.parents[1] / "data/ml_assets/checkpoint")
    parser.add_argument("--champion", type=Path, default=HERE.parents[1] / "data/ml_assets/champion")
    parser.add_argument("--min-new", type=int, default=20)
    parser.add_argument("--cooldown", type=int, default=21600)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--tolerance", type=float, default=.01)
    parser.add_argument("--force", action="store_true", help="Skip cooldown only; never skip labels or evaluation")
    args = parser.parse_args()
    if args.min_new < 1 or args.epochs < 1 or args.batch < 1 or args.cooldown < 0 or not 0 <= args.tolerance <= .05:
        parser.error("Invalid training/gate limits")
    for key in ("inbox", "state", "public", "data", "base", "champion"):
        setattr(args, key, getattr(args, key).resolve())
    if not args.inbox.is_dir():
        parser.error("Inbox does not exist")
    # Prevent accidental publication of private data through the static mount.
    for private in (args.inbox, args.state, args.data, args.base):
        if args.public == private or args.public in private.parents or private in args.public.parents:
            parser.error("Public and private directories must be separate")
    logging.basicConfig(level=logging.INFO)
    print(cycle(args))


if __name__ == "__main__":
    main()
