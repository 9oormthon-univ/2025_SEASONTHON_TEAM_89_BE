#!/usr/bin/env python3
"""Install the released bootstrap model. Existing local datasets are never overwritten."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ML = ROOT / "ml/phishing-classifier"
URL = "https://github.com/9oormthon-univ/2025_SEASONTHON_TEAM_89_BE/releases/download/ml-bootstrap-0.2.14/weheome-ml-bootstrap-0.2.14.zip"
SHA256 = "80491c93c26f10fb9f4a242cf226de13b1fb0b3d042f71f1c8334f3ef126432d"


def seed_data(target):
    if target.exists() and any(target.iterdir()):
        raise ValueError("Existing datasets will not be overwritten")
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "synthetic.jsonl"
        subprocess.run([sys.executable, str(ML / "generate_synthetic.py"), "--per-class", "1500",
                        "--seed", "890214", "--out", str(source)], check=True)
        sys.path.insert(0, str(ML))
        from auto_train import clean_rows
        rows = clean_rows([json.loads(line) for line in source.read_text().splitlines()])
    splits = {name: [] for name in ("train", "val", "test", "golden")}
    rng = random.Random(890214)
    for label in ("정상", "주의", "위험"):
        group = [row for row in rows if row["label"] == label]
        rng.shuffle(group)
        n = len(group)
        a, b, c = int(n * .7), int(n * .8), int(n * .9)
        for name, selected in zip(splits, (group[:a], group[a:b], group[b:c], group[c:])):
            splits[name].extend(selected)
    for name, rows in splits.items():
        (target / f"{name}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    (target / "PROVENANCE.txt").write_text(
        "Synthetic bootstrap only; not a real-world quality benchmark. Seed=890214. "
        "Replace with privately held reviewed train/val/test/golden datasets for production evaluation.\n")


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use an already downloaded bootstrap ZIP")
    parser.add_argument("--seed-data", action="store_true", help="Create synthetic-only starter datasets")
    args = parser.parse_args()
    sys.path.insert(0, str(ML))
    from auto_train import publish

    assets = ROOT / "data/ml_assets"
    public = Path(os.environ.get("ML_MODEL_PUBLIC_DIR", str(ROOT / "data/ml_public"))).resolve()
    dataset = Path(os.environ.get("ML_TRAIN_DATA_DIR", str(assets / "data"))).resolve()
    assets.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=assets) as temporary:
        temp = Path(temporary)
        archive = args.archive
        if archive is None:
            archive = temp / "bootstrap.zip"
            urllib.request.urlretrieve(URL, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise ValueError("Bootstrap archive checksum mismatch")
        expected = {"champion/phishing.onnx", "champion/vocab.txt"} | {
            "checkpoint/" + name for name in ("config.json", "model.safetensors", "special_tokens_map.json",
                                               "tokenizer.json", "tokenizer_config.json", "vocab.txt")}
        with zipfile.ZipFile(archive) as bundle:
            if set(bundle.namelist()) != expected:
                raise ValueError("Unexpected bootstrap archive content")
            for name in expected:
                destination = temp / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(name) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
        for name in ("champion", "checkpoint"):
            if not (assets / name).exists():
                shutil.move(str(temp / name), str(assets / name))
        if not (public / "latest.json").exists():
            publish(public, assets / "champion/phishing.onnx", assets / "champion/vocab.txt",
                    {"source": "Android 0.2.13 bundled classifier; no new training"},
                    version="bundled-0.2.13")
    if args.seed_data:
        seed_data(dataset)
    print("Bootstrap model ready. Start the backend normally: python -m app")
    if not all((dataset / f"{name}.jsonl").is_file() for name in ("train", "val", "test", "golden")):
        print("Auto training awaits private datasets in ML_TRAIN_DATA_DIR; --seed-data creates a synthetic-only starter set.")


if __name__ == "__main__":
    main()
