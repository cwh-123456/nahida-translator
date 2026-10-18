"""引擎基准测试：翻译若干句子，打印耗时与译文（需要 llama-server 已在运行）。"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config  # noqa: E402
from app.engine import Translator  # noqa: E402

SAMPLES = [
    "The quick brown fox jumps over the lazy dog.",
    "Machine translation has improved dramatically over the past few years.",
    "Please confirm the delivery date by replying to this email before Friday.",
    "The committee concluded that further investment in renewable energy is both economically viable and environmentally necessary.",
    "I'll be there in five minutes.",
    "Click the Settings icon in the top-right corner, then select Privacy and Security.",
]

cfg = config.load_config()
translator = Translator(cfg)
total = 0.0
for sentence in SAMPLES:
    t0 = time.time()
    result = translator.translate(sentence)
    elapsed = time.time() - t0
    total += elapsed
    print(f"[{elapsed:4.2f}s] {sentence}")
    print(f"        → {result.text}")
    print()
print(f"合计 {total:.2f}s，平均 {total / len(SAMPLES):.2f}s/句")
