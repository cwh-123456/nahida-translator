"""真模型切分质量回归：粘连的 PDF 数学文本能不能被切对。

这是「补空格」里唯一无法用 stub 验证的部分——判定逻辑和"只加空格"的校验
都在 test_glued_text.py 里用假模型测过了，但"切得对不对"必须让真模型跑。

背景：qwen2.5-7b 遇到整段全粘连的数学文本时，会把 RowjofUisa 切成
"Rowjo fUi sa"（切错但字符没变，_squeeze 校验拦不住），译文也跟着跑偏。
靠提示词里的 few-shot 示例 + "单字母变量必须独立成词"规则修好，此处钉住。

引擎没在跑就跳过——本测试不负责拉起 llama-server（那样太重）。
用法：python tests/test_space_restore_quality.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.engine import Translator, _squeeze  # noqa: E402

# 用户实际遇到的那段（PDF 复制出来整段没有空格）
GLUED = (
    "RowjofUisacombinationofrows1,...,jofA(whentherearenorowexchanges)."
    "IfAx=0thenUx=0(nottrueifbreplaces0).UjustkeepsthediagonalofA"
    "whenAislowertriangular,allentriesbelowthatdiagonalgotozero."
)

# 关键片段：变量要单独成词，短语要断开
NEEDLES = [
    "Row j of U is a combination of rows",
    "when A is lower triangular",
    "the diagonal of A",
]


def main() -> int:
    tr = Translator(config.load_config())
    if not tr._health():
        print("引擎没在运行，跳过（本测试不负责启动 llama-server）")
        return 0

    out = tr.restore_spaces(GLUED)
    print("原文还原 ->")
    print("   ", out)
    print()

    results = []
    same = _squeeze(out) == _squeeze(GLUED)
    results.append(same)
    print(f"    字符序列未变   {'✓' if same else '✗'}")

    for needle in NEEDLES:
        ok = needle in out
        results.append(ok)
        print(f"    含「{needle}」{'✓' if ok else '✗'}")

    print("\n结果:", "PASS" if all(results) else "FAIL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
