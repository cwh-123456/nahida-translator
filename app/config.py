"""配置加载 / 保存。所有相对路径都相对项目根目录。"""
from __future__ import annotations

import copy
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LEGACY_CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULTS = {
    "server": {
        "exe": r"runtime\llama\llama-server.exe",
        "model": r"models\qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
        "host": "127.0.0.1",
        "port": 8377,
        "context": 4096,
        "gpu_layers": 99,
        "threads": 0,
        "startup_timeout": 150,
    },
    "translate": {
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 1024,
        "system_prompt": (
            "你是一个专业的英译中翻译引擎。严格遵守以下规则：\n"
            "1. 只输出中文译文本身，不要输出原文、解释、注释、译注或任何前后缀。\n"
            "2. 译文要准确、通顺、自然，符合中文表达习惯，不要生硬直译。\n"
            "3. 保留原文中的数字、公式、代码、链接；专有名词用通行的中文译名，必要时在后面括号保留英文原名。\n"
            "4. 原文口语化时译文也保持口语化；原文正式时译文也保持正式。\n"
            "5. 如果原文本身已经是中文，原样输出；如果原文不完整，就按字面意思翻译。\n"
            "6. 不要使用 Markdown 代码块包裹译文。\n"
            "7. 必须使用简体中文，不要使用繁体字。\n"
            "8. 译文中不得出现英文单词或英文短语，也不要中英混杂（代码、公式、人名、产品名等确实没有"
            "通行中文译法的除外）；出现这类名称时按中文习惯说明。\n"
            "9. 直接给出完整译文，不要省略、不要用“…”代替。"
        ),
    },
    "ui": {
        "icon_size": 38,
        "icon_timeout": 5.0,
        "dock_size": 46,
        "popup_width": 700,
        "popup_height": 850,
        "font_family": "Microsoft YaHei UI",
        "font_size": 11,
        "source_font_size": 9,
        "show_source": True,
        "copy_on_finish": False,
        "window_pos": None,
        "mascot_enabled": True,
        "mascot_size": 180,
        "mascot_pos": None,
        "mascot_gif": r"assets\nahida_swing.gif",
        "panel_bg": True,
        "panel_alpha": 0.72,
        "panel_click_through": False,
        "panel_auto_passthrough": True,
    },
    "hotkey": "<ctrl>+<alt>+t",
    "mode": "auto",
    "pause": False,
    "autostart": False,
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    data = {}
    # 迁移旧版本放在根目录的 config.json
    if not os.path.exists(CONFIG_PATH) and os.path.exists(LEGACY_CONFIG_PATH):
        try:
            with open(LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        try:
            os.remove(LEGACY_CONFIG_PATH)
        except Exception:
            pass
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    cfg = _merge(DEFAULTS, data)
    if not os.path.exists(CONFIG_PATH):
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def abspath(rel: str) -> str:
    """把配置里的相对路径解析为绝对路径（已绝对则原样返回）。"""
    if os.path.isabs(rel):
        return rel
    return os.path.normpath(os.path.join(ROOT, rel))


def ensure_dirs() -> str:
    logs = os.path.join(ROOT, "logs")
    os.makedirs(logs, exist_ok=True)
    return logs
