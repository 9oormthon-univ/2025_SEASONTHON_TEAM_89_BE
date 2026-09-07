import csv
import json
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_train import gate, prepare_run, publish, read_feedback
from mobile_eval import encode, metrics


def write_csv(path, rows):
    with path.open("w") as output:
        writer = csv.DictWriter(output, fieldnames=["text", "label", "engine_verdict"])
        writer.writeheader()
        writer.writerows(rows)
    os.utime(path, (time.time() - 10, time.time() - 10))


def test_feedback_excludes_unlabeled_duplicates_and_conflicts(tmp_path):
    write_csv(tmp_path / "a.csv", [
        {"text": "unreviewed message", "label": "", "engine_verdict": "위험"},
        {"text": "reviewed message", "label": "정상"},
        {"text": "reviewed message", "label": "정상"},
        {"text": "conflicting message", "label": "정상"},
        {"text": "conflicting message", "label": "위험"},
        {"text": "연락 010-1234-5678", "label": "위험"},
    ])
    result = read_feedback(tmp_path)
    assert len(result) == 2
    assert all("010-1234-5678" not in r["text"] for r in result)
    assert {r["label"] for r in result} == {"정상", "위험"}


def test_holdouts_never_enter_training(tmp_path):
    data, output = tmp_path / "source", tmp_path / "run"
    data.mkdir()
    rows = [{"text": f"message {i}", "label": label} for i, label in enumerate(["정상", "주의", "위험"])]
    for split in ("train", "val", "test", "golden", "bench_web"):
        (data / f"{split}.jsonl").write_text("".join(
            json.dumps(dict(r, text=split + r["text"])) + "\n" for r in rows))
    feedback = [dict(rows[0], text="testmessage 0"), dict(rows[1], text="new reviewed")]
    eligible, _ = prepare_run(data, feedback, output)
    assert [r["text"] for r in eligible] == ["new reviewed"]
    assert "testmessage 0" not in (output / "train.jsonl").read_text()


def test_gate_blocks_recall_f1_and_false_positive_regressions():
    old = {"test": {"danger_recall": .9, "normal_fp": .05, "macro_f1": .85}}
    assert not gate(old, old)
    for metric, value in [("danger_recall", .8), ("normal_fp", .2), ("macro_f1", .7)]:
        new = {"test": dict(old["test"], **{metric: value})}
        assert gate(new, old)


def test_publish_is_immutable_and_manifest_hashes_match(tmp_path):
    model, vocab = tmp_path / "model", tmp_path / "vocab"
    model.write_bytes(b"model")
    vocab.write_text("[PAD]\n[UNK]\n[CLS]\n[SEP]")
    public = tmp_path / "public"
    manifest = publish(public, model, vocab, {}, "v1")
    assert manifest["files"]["phishing.onnx"]["size"] == 5
    assert json.loads((public / "latest.json").read_text()) == manifest
    with pytest.raises(ValueError):
        publish(public, model, vocab, {}, "v1")
    assert json.loads((public / "latest.json").read_text()) == manifest
    with pytest.raises(ValueError):
        publish(public, model, vocab, {}, "../bad")


def test_mobile_tokenizer_case_punctuation_and_padding():
    vocab = {p: i for i, p in enumerate(["[PAD]", "[UNK]", "[CLS]", "[SEP]", "Hello", ",", "한", "##글"])}
    ids, mask = encode("Hello, 한글", vocab, 8)
    assert ids == [2, 4, 5, 6, 7, 3, 0, 0]
    assert mask == [1, 1, 1, 1, 1, 1, 0, 0]
    assert encode("hello", vocab, 4)[0] == [2, 1, 3, 0]


def test_metrics_count_false_positives_and_missed_danger():
    result = metrics([0, 0, 1, 2, 2], [0, 1, 1, 2, 0])
    assert result["normal_fp"] == .5
    assert result["danger_recall"] == .5


def test_release_api_only_serves_models_and_never_private_files(tmp_path):
    from fastapi.testclient import TestClient
    from model_server import create_app

    client = TestClient(create_app(tmp_path / "public"))
    assert client.get("/api/ml/models/latest.json").status_code == 404
    model, vocab = tmp_path / "model", tmp_path / "vocab"
    model.write_bytes(b"model-data")
    vocab.write_text("[PAD]\n[UNK]\n[CLS]\n[SEP]")
    manifest = publish(tmp_path / "public", model, vocab, {}, "v1")
    response = client.get("/api/ml/models/latest.json")
    assert response.status_code == 200
    assert response.json() == manifest
    assert response.headers["cache-control"] == "no-store"
    response = client.get("/api/ml/models/releases/v1/phishing.onnx")
    assert response.content == b"model-data"
    assert client.get("/api/ml/models/releases/v1/metrics.json").status_code == 404
    assert client.get("/api/ml/models/releases/v1/train.csv").status_code == 404


def test_rejected_or_failed_training_does_not_publish(tmp_path, monkeypatch):
    import argparse
    import auto_train

    inbox, data, state, public = [tmp_path / n for n in ("inbox", "data", "state", "public")]
    inbox.mkdir()
    data.mkdir()
    for split in ("train", "val", "test", "golden"):
        (data / f"{split}.jsonl").write_text("".join(
            json.dumps({"text": f"{split} message {i}", "label": label}) + "\n"
            for i, label in enumerate(["정상", "주의", "위험"])))
    write_csv(inbox / "a.csv", [{"text": "fresh reviewed text", "label": "정상", "engine_verdict": "주의"}])
    args = argparse.Namespace(inbox=inbox, data=data, state=state, public=public, base=tmp_path,
                              champion=tmp_path, min_new=1, cooldown=0, force=True, epochs=1, batch=4, tolerance=.01)
    def failing_command(*_):
        raise RuntimeError("training failure")
    monkeypatch.setattr(auto_train, "command", failing_command)
    with pytest.raises(RuntimeError):
        auto_train.cycle(args)
    assert not (public / "latest.json").exists()
    assert json.loads((state / "status.json").read_text())["status"] == "failed"

    def fake_command(command, _):
        output = Path(command[command.index("--out") + 1])
        output.mkdir(parents=True, exist_ok=True)
        if "train.py" in command[1]:
            (output / "config.json").write_text(json.dumps({"id2label": {"0": "정상", "1": "주의", "2": "위험"}}))
            (output / "tokenizer_config.json").write_text('{"do_lower_case": false}')
    monkeypatch.setattr(auto_train, "command", fake_command)
    monkeypatch.setattr(auto_train, "evaluate", lambda model, *_: {
        "macro_f1": .1 if "export" in str(model) else .9, "danger_recall": .9, "normal_fp": .05})
    assert auto_train.cycle(args) == "rejected"
    assert not (public / "latest.json").exists()
    # The exact rejected corpus is not retrained on the next tick.
    assert auto_train.cycle(args) == "waiting_for_feedback"
