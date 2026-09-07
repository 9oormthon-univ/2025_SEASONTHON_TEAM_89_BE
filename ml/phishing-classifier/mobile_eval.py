"""Evaluate the exported artifact with the Android cased WordPiece/0.5 decision contract."""
from __future__ import annotations

import unicodedata
from pathlib import Path

from pii import scrub

LABELS = ["정상", "주의", "위험"]


def encode(text: str, vocab: dict[str, int], length: int = 128) -> tuple[list[int], list[int]]:
    # Kotlin iterates UTF-16 Char values (including surrogate pairs), not Unicode code points.
    raw = text.encode("utf-16-le", errors="surrogatepass")
    chars = [chr(int.from_bytes(raw[i:i + 2], "little")) for i in range(0, len(raw), 2)]
    cleaned = []
    for c in chars:
        cp, category = ord(c), unicodedata.category(c)
        if cp in (0, 0xFFFD) or (category in ("Cc", "Cf") and c not in "\t\n\r"):
            continue
        if c in " \t\n\r" or category in ("Zs", "Zl", "Zp"):
            cleaned.append(" ")
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            cleaned.extend((" ", c, " "))
        else:
            cleaned.append(c)
    tokens = []
    for raw_token in "".join(cleaned).split(" "):
        current = ""
        for c in raw_token:
            cp = ord(c)
            punctuation = (33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96
                           or 123 <= cp <= 126 or unicodedata.category(c).startswith("P"))
            if punctuation:
                if current:
                    tokens.append(current)
                    current = ""
                tokens.append(c)
            else:
                current += c
        if current:
            tokens.append(current)
    pieces = []
    for token in tokens:
        word, start = [], 0
        if len(token) > 100:
            pieces.append("[UNK]")
            continue
        while start < len(token):
            found = None
            for end in range(len(token), start, -1):
                piece = ("##" if start else "") + token[start:end]
                if piece in vocab:
                    found = (piece, end)
                    break
            if found is None:
                word = ["[UNK]"]
                break
            word.append(found[0])
            start = found[1]
        pieces.extend(word)
    ids = [vocab["[CLS]"]] + [vocab.get(p, vocab["[UNK]"]) for p in pieces[:length - 2]] + [vocab["[SEP]"]]
    return ids + [vocab["[PAD]"]] * (length - len(ids)), [1] * len(ids) + [0] * (length - len(ids))


def metrics(truth: list[int], predicted: list[int]) -> dict:
    assert len(truth) == len(predicted) and truth
    f1 = []
    for label in sorted(set(truth)):
        tp = sum(t == label and p == label for t, p in zip(truth, predicted))
        fp = sum(t != label and p == label for t, p in zip(truth, predicted))
        fn = sum(t == label and p != label for t, p in zip(truth, predicted))
        f1.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0)
    result = {"macro_f1": sum(f1) / len(f1), "n": len(truth)}
    if 2 in truth:
        result["danger_recall"] = sum(t == 2 and p == 2 for t, p in zip(truth, predicted)) / truth.count(2)
    if 0 in truth:
        result["normal_fp"] = sum(t == 0 and p != 0 for t, p in zip(truth, predicted)) / truth.count(0)
    return result


def evaluate(model: Path, vocab_path: Path, rows: list[dict]) -> dict:
    import numpy as np
    import onnxruntime as ort

    vocab = {t: i for i, t in enumerate(vocab_path.read_text().splitlines())}
    assert all(t in vocab for t in ("[UNK]", "[CLS]", "[SEP]", "[PAD]"))
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
    names = {i.name for i in session.get_inputs()}
    assert "input_ids" in names and names <= {"input_ids", "attention_mask", "token_type_ids"}
    truth, predicted = [], []
    for row in rows:
        ids, mask = encode(scrub(row["text"]), vocab)
        values = {"input_ids": ids, "attention_mask": mask, "token_type_ids": [0] * 128}
        logits = session.run(None, {n: np.array([values[n]], dtype=np.int64) for n in names})[0]
        assert logits.shape == (1, 3) and np.isfinite(logits).all()
        probs = np.exp(logits[0] - logits[0].max())
        probs /= probs.sum()
        caution, danger = probs[1], probs[2]
        predicted.append(2 if danger >= .5 and danger >= caution else 1 if caution >= .5 else 0)
        truth.append(LABELS.index(row["label"]))
    return metrics(truth, predicted)
