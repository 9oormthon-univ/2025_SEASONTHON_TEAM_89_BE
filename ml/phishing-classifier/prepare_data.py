#!/usr/bin/env python3
"""데이터 병합·정제·분할 (순수 표준 라이브러리 — 바로 실행 가능).

입력:
  · data/synthetic.jsonl          (generate_synthetic.py 산출)
  · data/sources/*.jsonl          (실데이터: AI Hub / 사용자 신고 등, 같은 스키마)
출력:
  · data/train.jsonl / val.jsonl / test.jsonl  (계층 분할 80/10/10)

각 줄 스키마: {"text": str, "label": "정상|주의|위험", "type"?: str, "stage"?: str}

정제:
  · 텍스트 trim + 빈/초단문(<2자) 제거
  · 텍스트 기준 전역 중복 제거(실데이터 우선 보존)
  · 클래스별 계층 분할로 train/val/test 분포 유지

사용:
  python prepare_data.py --seed 42 --val 0.1 --test 0.1
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random

from labels import RISK_LABELS
from pii import scrub

DATA_DIR = "data"


def load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--val", type=float, default=0.1)
    ap.add_argument("--test", type=float, default=0.1)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    # 실데이터(sources)를 먼저 넣어 중복 시 우선 보존
    paths: list[str] = sorted(glob.glob(os.path.join(DATA_DIR, "sources", "*.jsonl")))
    synth = os.path.join(DATA_DIR, "synthetic.jsonl")
    if os.path.exists(synth):
        paths.append(synth)
    if not paths:
        raise SystemExit("입력 없음: data/synthetic.jsonl 또는 data/sources/*.jsonl 를 먼저 준비하세요.")

    seen: set[str] = set()
    # golden 회귀셋(data/golden.jsonl)은 **학습에서 제외(held-out)** — 텍스트를 미리 seen 에 넣어
    # train/val/test 어디에도 들어가지 않게 한다. 그래야 golden 으로 한 평가가 누수 없는 회귀 게이트가 된다.
    golden_path = os.path.join(DATA_DIR, "golden.jsonl")
    n_held = 0
    if os.path.exists(golden_path):
        for r in load_jsonl(golden_path):
            t = scrub((r.get("text") or "").strip())
            if len(t) >= 2:
                seen.add(t)
                n_held += 1
    by_class: dict[str, list[dict]] = {c: [] for c in RISK_LABELS}
    n_raw = 0
    for p in paths:
        for r in load_jsonl(p):
            n_raw += 1
            # PII 마스킹 — 모든 학습 소스에 일관 적용(프라이버시 + 일반화). 온디바이스 분류기도 동일 치환.
            text = scrub((r.get("text") or "").strip())
            label = r.get("label")
            if len(text) < 2 or label not in RISK_LABELS or text in seen:
                continue
            seen.add(text)
            by_class[label].append({"text": text, "label": label,
                                    "type": r.get("type"), "stage": r.get("stage"),
                                    "origin": r.get("origin") or "unknown"})

    train, val, test = [], [], []
    for label, rows in by_class.items():
        rng.shuffle(rows)
        n = len(rows)
        n_test = int(n * args.test)
        n_val = int(n * args.val)
        test += rows[:n_test]
        val += rows[n_test:n_test + n_val]
        train += rows[n_test + n_val:]

    for split in (train, val, test):
        rng.shuffle(split)

    def dump(name: str, rows: list[dict]) -> None:
        with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    dump("train.jsonl", train)
    dump("val.jsonl", val)
    dump("test.jsonl", test)

    def dist(rows: list[dict]) -> str:
        c = {k: 0 for k in RISK_LABELS}
        for r in rows:
            c[r["label"]] += 1
        return " / ".join(f"{k} {c[k]}" for k in RISK_LABELS)

    def origin_dist(rows: list[dict]) -> str:
        c: dict[str, int] = {}
        for r in rows:
            o = r.get("origin") or "unknown"
            c[o] = c.get(o, 0) + 1
        return " / ".join(f"{k} {v}" for k, v in sorted(c.items()))

    if n_held:
        print(f"golden held-out 제외: {n_held}행 (train/val/test 어디에도 안 들어감)")
    print(f"원본 {n_raw}행 → 중복제거 후 {len(seen) - n_held}행")
    print(f"train {len(train)}  ({dist(train)})")
    print(f"val   {len(val)}  ({dist(val)})")
    print(f"test  {len(test)}  ({dist(test)})")
    print(f"출처(origin) 분포(train): {origin_dist(train)}")


if __name__ == "__main__":
    main()
