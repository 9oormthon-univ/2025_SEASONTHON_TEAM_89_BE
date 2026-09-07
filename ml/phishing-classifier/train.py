#!/usr/bin/env python3
"""피싱 분류기 파인튜닝 (HuggingFace Trainer) — 클래스 불균형 대응 포함.

한국어 인코더를 3-class(정상/주의/위험)로 파인튜닝. prepare_data.py 가 만든
data/{train,val}.jsonl 을 사용한다.

기본 베이스 = **KoELECTRA-small-v3 (Apache-2.0)** — 상업 재배포에 깨끗하고 경량(~14M),
WordPiece 토크나이저라 앱의 WordPieceTokenizer와 호환.

⚠️ 위험(positive)은 희소·정상(negative)은 다수 → 불균형. 그냥 CE로 학습하면 '정상'으로 쏠려
   위험 recall이 낮아진다. 그래서:
   · --loss weighted (기본): 클래스 빈도 역수 가중 CE (희소 클래스 손실↑)
   · --loss focal: focal loss(쉬운 예제 down-weight, 어려운/희소 예제 집중) + 클래스 가중
   · --loss ce: 비가중(베이스라인 비교용)

사용:
  pip install -r requirements.txt
  python train.py --base monologg/koelectra-small-v3-discriminator --epochs 4 --loss weighted

지표: macro-F1 + 클래스별 precision/recall. (운영 임계값은 evaluate.py 에서 보정)
"""
from __future__ import annotations

import argparse
from pathlib import Path
from collections import Counter

import numpy as np
import torch
from datasets import load_dataset
from sklearn.metrics import classification_report, f1_score
from torch import nn
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

from labels import ID_TO_RISK, RISK_LABELS, RISK_TO_ID

# 출처별 학습 가중(--reweight-origin). 동의받은 실데이터는 우대, 합성 시드는 down-weight,
# KorCCVi는 실데이터지만 통화체 도메인 갭이라 약간 낮춤. (prepare_data 가 origin 을 보존)
ORIGIN_WEIGHTS = {
    "user_confirmed": 1.5,
    "report": 1.5,
    "counterscam112": 1.2,   # 경찰 공개 실제 사기 스크립트 — 고신뢰 위험 원문
    "web": 1.2,              # 금감원/경찰/KISA 등 인용 실제 사례 원문 — 고신뢰
    "nsmc": 1.0,
    "notice": 1.0,
    "nikl": 0.9,             # 실대화지만 구어 전사 도메인 (키보드 입력과 약간의 갭)
    "korccvi": 0.8,
    "korccvi_call": 0.8,
    "synthetic": 0.5,
    "unknown": 1.0,
}


class ImbalanceTrainer(Trainer):
    """compute_loss 를 가중 CE / focal loss 로 교체. (Trainer 버전별 추가 인자는 **kwargs 로 흡수)"""

    def __init__(self, *args, class_weights=None, loss_type="weighted", focal_gamma=2.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._class_weights = class_weights
        self._loss_type = loss_type
        self._focal_gamma = focal_gamma

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):  # noqa: ANN001
        labels = inputs.pop("labels")
        sample_w = inputs.pop("sample_weight", None)  # 출처별 가중(없으면 균등)
        outputs = model(**inputs)
        logits = outputs.logits
        weight = None
        if self._class_weights is not None and self._loss_type != "ce":
            weight = self._class_weights.to(logits.device)

        ce = nn.functional.cross_entropy(logits, labels, weight=weight, reduction="none")
        if self._loss_type == "focal":
            pt = torch.exp(-ce)  # 정답 클래스 확률
            ce = (1.0 - pt) ** self._focal_gamma * ce

        if sample_w is not None:
            sw = sample_w.to(ce.device).float()
            loss = (ce * sw).sum() / sw.sum().clamp_min(1e-8)  # 출처-가중 평균
        else:
            loss = ce.mean()

        return (loss, outputs) if return_outputs else loss


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="monologg/koelectra-small-v3-discriminator")
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--max-len", type=int, default=128)
    ap.add_argument("--loss", choices=["weighted", "focal", "ce"], default="weighted",
                    help="weighted=빈도역수 가중CE / focal=focal loss+가중 / ce=비가중(베이스라인)")
    ap.add_argument("--focal-gamma", type=float, default=2.0)
    ap.add_argument("--reweight-origin", action="store_true",
                    help="출처별 샘플 가중(동의 실데이터 우대·합성 down-weight). origin 필드 사용")
    ap.add_argument("--out", default="out")
    ap.add_argument("--data-dir", default="data", help="격리된 학습/검증 JSONL 디렉터리")
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.base,
        num_labels=len(RISK_LABELS),
        id2label=ID_TO_RISK,
        label2id=RISK_TO_ID,
    )

    ds = load_dataset(
        "json",
        data_files={split: str(Path(args.data_dir) / f"{split}.jsonl") for split in ("train", "val")},
    )

    # 클래스 가중치 = 빈도 역수(정규화). 인코딩 전 라벨 분포로 계산.
    train_label_ids = [RISK_TO_ID[l] for l in ds["train"]["label"]]
    counts = Counter(train_label_ids)
    n, k = len(train_label_ids), len(RISK_LABELS)
    class_weights = torch.tensor(
        [n / (k * max(counts.get(i, 0), 1)) for i in range(k)],
        dtype=torch.float,
    )
    print("클래스 분포:", {RISK_LABELS[i]: counts.get(i, 0) for i in range(k)})
    print(f"클래스 가중치({args.loss}):", {RISK_LABELS[i]: round(class_weights[i].item(), 3) for i in range(k)})

    def encode(batch: dict) -> dict:
        enc = tok(batch["text"], truncation=True, max_length=args.max_len, padding="max_length")
        enc["labels"] = [RISK_TO_ID[l] for l in batch["label"]]
        if args.reweight_origin:
            origins = batch.get("origin") or ["unknown"] * len(batch["label"])
            enc["sample_weight"] = [float(ORIGIN_WEIGHTS.get(o or "unknown", 1.0)) for o in origins]
        else:
            enc["sample_weight"] = [1.0] * len(batch["label"])
        return enc

    ds = ds.map(encode, batched=True, remove_columns=ds["train"].column_names)

    def compute_metrics(p) -> dict:
        preds = np.argmax(p.predictions, axis=1)
        return {"macro_f1": f1_score(p.label_ids, preds, average="macro")}

    trainer = ImbalanceTrainer(
        model=model,
        args=TrainingArguments(
            output_dir=args.out,
            per_device_train_batch_size=args.batch,
            per_device_eval_batch_size=args.batch,
            num_train_epochs=args.epochs,
            learning_rate=args.lr,
            eval_strategy="epoch",
            save_strategy="epoch",
            load_best_model_at_end=True,
            metric_for_best_model="macro_f1",
            fp16=torch.cuda.is_available(),
            logging_steps=50,
        ),
        train_dataset=ds["train"],
        eval_dataset=ds["val"],
        compute_metrics=compute_metrics,
        class_weights=class_weights,
        loss_type=args.loss,
        focal_gamma=args.focal_gamma,
    )
    trainer.train()
    trainer.save_model(args.out)
    tok.save_pretrained(args.out)

    # 검증셋 상세 리포트
    pred = trainer.predict(ds["val"])
    y_pred = np.argmax(pred.predictions, axis=1)
    print(classification_report(pred.label_ids, y_pred, target_names=RISK_LABELS, digits=4))
    print(f"\n저장 완료 → {args.out}")
    print("다음: python evaluate.py --model out  (클래스별 임계값 보정 + 혼동행렬)")


if __name__ == "__main__":
    main()
