"""配置加载 / 保存。所有相对路径都相对项目根目录。"""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import tempfile
import threading
import time

log = logging.getLogger("config")

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
            "9. 直接给出完整译文：原文的每个句子、每个条件／原因／让步从句"
            "（when、if、unless、while 引导的）都要在译文中体现，"
            "不要合并句子、不要省略从句、不要用“…”代替。"
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
    # 图片识别引擎：独立进程、纯 CPU（8G 显存放不下两套模型），按需启动、闲置自动退出
    "vision": {
        "exe": r"runtime\llama\llama-server.exe",
        "model": r"models\vision\Qwen3VL-4B-Instruct-Q4_K_M.gguf",
        "mmproj": r"models\vision\mmproj-Qwen3VL-4B-Instruct-F16.gguf",
        "host": "127.0.0.1",
        "port": 8378,
        "context": 8192,
        "gpu_layers": 0,
        "threads": 0,
        "startup_timeout": 120,
        "idle_timeout": 600,
        "max_image_side": 1600,
        "min_image_tokens": 1024,
    },
    "hotkey": "<ctrl>+<alt>+t",
    # 截图翻译热键。用 RegisterHotKey 独占注册，被占用时自动顺延到后面的备用键
    "hotkey_shot": "ctrl+alt+d",
    "hotkey_shot_fallbacks": ["ctrl+alt+k", "ctrl+alt+q"],
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


# ---------------------------------------------------------------- 内部工具
# 所有写盘都从这里走：同一把锁串行化，先写临时文件再原子替换，
# 避免托盘线程 / Tk 线程同时写导致内容交错，或写到一半崩溃留下半个 JSON。
_SAVE_LOCK = threading.RLock()
_MISSING = object()


def _get_path(data: dict, path: str):
    """按 "ui.mascot_enabled" 这样的路径取值，不存在返回 _MISSING。"""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _set_path(data: dict, path: str, value) -> None:
    """按点号路径写入，缺失的中间层自动补空字典。"""
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = copy.deepcopy(value)


def _sanitize(data: dict) -> dict:
    """按 DEFAULTS 的白名单过滤，避免把程序内部的临时键或错误类型写进文件。"""
    out: dict = {}
    dropped: list[str] = []
    for key, value in (data or {}).items():
        default = DEFAULTS.get(key, _MISSING)
        if default is _MISSING:
            dropped.append(key)
            continue
        if isinstance(default, dict):
            if not isinstance(value, dict):
                dropped.append(key)
                continue
            sub: dict = {}
            for sub_key, sub_value in value.items():
                if sub_key in default:
                    sub[sub_key] = sub_value
                else:
                    dropped.append(f"{key}.{sub_key}")
            out[key] = sub
        elif isinstance(value, dict):
            # 默认值是标量却收到字典，写进去会让下次加载出错
            dropped.append(key)
        else:
            out[key] = value
    if dropped:
        log.warning("保存配置时丢弃了不认识的字段: %s", ", ".join(sorted(dropped)))
    return out


def _read_raw() -> dict:
    """读磁盘上的原始配置（不合并默认值）。文件缺失或损坏时回退到默认值副本。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        log.error("配置文件顶层不是 JSON 对象，将按默认值处理")
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.error("读取配置文件失败，将按默认值处理: %s", exc)
    return copy.deepcopy(DEFAULTS)


def _atomic_write(path: str, text: str) -> None:
    """同目录临时文件 + fsync + os.replace，保证文件要么是旧内容要么是新内容。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _backup_broken_config() -> str | None:
    """把无法解析的配置文件另存一份，避免用户配置被无声丢弃。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = f"{CONFIG_PATH}.bad-{stamp}"
    seq = 1
    while os.path.exists(dst):
        seq += 1
        dst = f"{CONFIG_PATH}.bad-{stamp}-{seq}"
    try:
        shutil.copy2(CONFIG_PATH, dst)
        return dst
    except Exception:
        return None


def load_config() -> dict:
    data = {}
    # 迁移旧版本放在根目录的 config.json
    if not os.path.exists(CONFIG_PATH) and os.path.exists(LEGACY_CONFIG_PATH):
        try:
            with open(LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            log.error("旧版配置文件解析失败: %s", exc)
            data = {}
        try:
            os.remove(LEGACY_CONFIG_PATH)
        except Exception:
            pass
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("顶层不是 JSON 对象")
            data = loaded
        except Exception as exc:
            data = {}
            backup = _backup_broken_config()
            log.error(
                "配置文件无法解析（%s），已备份到 %s，本次使用默认设置",
                exc, backup or "（备份失败）",
            )
    cfg = _merge(DEFAULTS, data)
    if not os.path.exists(CONFIG_PATH):
        save_config(cfg)
    elif _sanitize(data) != _sanitize(cfg):
        # 版本升级新增了配置项（例如 vision 段、hotkey_shot）：补进文件，
        # 否则用户打开 config.json 根本看不到这些项，也就无从修改。
        # _sanitize 只保留 DEFAULTS 里有的键，所以旧版遗留的废弃字段也会顺手清掉。
        log.info("配置缺少新版本的默认项，已补充写回")
        save_config(cfg)
    return cfg


def save_config(cfg: dict, keys=None) -> bool:
    """保存配置。

    keys=None            —— 整份写回（兼容旧调用）。
    keys=["ui.mascot_enabled", ...] —— 只把这几项合并进磁盘现值，其余字段保持磁盘原样。

    之所以要有 keys 参数：进程内存里的 cfg 可能被临时改过（测试脚本、实验性功能、
    异常回滚等），整份写回会把这些临时值一起固化到文件里。指定 keys 后，
    只有用户真正操作的那几个字段会落盘。
    """
    with _SAVE_LOCK:
        try:
            if keys is None:
                data = copy.deepcopy(cfg)
            else:
                data = _read_raw()
                for key in keys:
                    value = _get_path(cfg, key)
                    if value is _MISSING:
                        log.warning("保存配置时找不到字段 %s，已跳过", key)
                        continue
                    _set_path(data, key, value)
            text = json.dumps(_sanitize(data), ensure_ascii=False, indent=2)
            _atomic_write(CONFIG_PATH, text)
            return True
        except Exception as exc:
            log.error("保存配置失败（磁盘写入异常）: %s", exc)
            return False


def abspath(rel: str) -> str:
    """把配置里的相对路径解析为绝对路径（已绝对则原样返回）。"""
    if os.path.isabs(rel):
        return rel
    return os.path.normpath(os.path.join(ROOT, rel))


def ensure_dirs() -> str:
    logs = os.path.join(ROOT, "logs")
    os.makedirs(logs, exist_ok=True)
    return logs
