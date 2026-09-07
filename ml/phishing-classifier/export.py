#!/usr/bin/env python3
"""학습 모델 → ONNX 내보내기 + INT8 동적 양자화 (온디바이스 배치용).

1) optimum-cli 로 text-classification ONNX export → model.onnx (fp32)
2) onnxruntime `quantize_dynamic` 로 INT8 가중치 양자화 → model.int8.onnx (약 1/4 크기)

⚠️ `python -m onnxruntime.quantization.preprocess`(symbolic shape inference)는 RoBERTa의
   동적 시퀀스 길이에서 'Incomplete symbolic shape inference'로 실패한다 — **그 단계는 건너뛴다.**
   동적 양자화(quantize_dynamic)는 preprocess가 필요 없고 가중치만 INT8로 바꾼다.

사용:
  python export.py --model out --out export/onnx
산출물: export/onnx/{model.onnx(fp32), model.int8.onnx, vocab.txt, tokenizer*.json}
앱 배치: model.int8.onnx → filesDir/models/phishing.onnx,  vocab.txt → filesDir/models/vocab.txt
"""
from __future__ import annotations

import argparse
import os
import subprocess


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="out")
    ap.add_argument("--out", default="export/onnx")
    ap.add_argument("--fmt", choices=["onnx"], default="onnx", help="(tflite는 optimum-cli/ai-edge-torch 별도)")
    ap.add_argument("--no-quantize", action="store_true", help="INT8 양자화 건너뛰고 fp32만")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    # 1) ONNX export (optimum)
    cmd = ["optimum-cli", "export", "onnx", "--model", args.model,
           "--task", "text-classification", args.out]
    print("실행:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    fp32 = os.path.join(args.out, "model.onnx")
    print(f"ONNX export 완료 → {fp32}  ({os.path.getsize(fp32) / 1e6:.1f}MB)")

    if args.no_quantize:
        return

    # 2) INT8 동적 양자화 — preprocess(symbolic shape inference) 없이 가중치만 양자화.
    from onnxruntime.quantization import QuantType, quantize_dynamic

    int8 = os.path.join(args.out, "model.int8.onnx")
    quantize_dynamic(fp32, int8, weight_type=QuantType.QInt8)
    print(f"INT8 양자화 완료 → {int8}  "
          f"({os.path.getsize(fp32) / 1e6:.1f}MB → {os.path.getsize(int8) / 1e6:.1f}MB)")
    print("앱 배치(ADB):")
    print(f"  adb push {int8} /sdcard/ && adb push {os.path.join(args.out, 'vocab.txt')} /sdcard/")
    print("  adb shell run-as com.weheome.app sh -c "
          "'mkdir -p files/models; cp /sdcard/model.int8.onnx files/models/phishing.onnx; "
          "cp /sdcard/vocab.txt files/models/vocab.txt'")


if __name__ == "__main__":
    main()
