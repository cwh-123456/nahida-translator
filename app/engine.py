"""本地翻译引擎：管理 llama-server 子进程（本机回环，不联网、不需要任何 API Key）。

首次使用时自动拉起 llama-server 把模型载入显存；之后每句翻译就是一次
本机 127.0.0.1 的流式请求，GPU 推理，完全离线。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("engine")

CREATE_NO_WINDOW = 0x08000000
# 本机回环绝对不走系统代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CLEAN_PREFIX = re.compile(
    r"^\s*(?:译文|翻译结果|中文译文|Translation|Translated text)\s*[:：]\s*", re.I
)
CLEAN_WRAP = re.compile(r"^\s*(?:\"|“|「|『)(.*)(?:\"|”|」|』)\s*$", re.S)

# 单词详解的提示词（词典式输出，格式固定，便于阅读）
EXPLAIN_PROMPT = (
    "你是一部严谨的英汉词典。用户会给你一个英文单词，请输出词典式讲解，严格按下面的纯文本格式：\n"
    "音标：/.../\n"
    "词性：...\n"
    "释义：1. ... 2. ...\n"
    "常见搭配：...\n"
    "例句：\n"
    "1. 英文例句 —— 中文译文\n"
    "2. 英文例句 —— 中文译文\n"
    "要求：只用简体中文解释；不要 Markdown 符号、不要表格、不要寒暄；内容准确精炼。"
    "例句必须包含该单词本身或其变形，句前不要加 English: 之类的标签；"
    "如果单词有多种词性，分别列出，但整体保持简洁；不要输出与该词无关的内容。"
)

# 单个英文单词（允许连字符、撇号，结尾允许一个标点）
_SINGLE_WORD = re.compile(r"^[A-Za-z][A-Za-z'’\-]{0,39}(?:\s*[.,;:!?])?$")

ZH2EN_PROMPT = (
    "你是一个专业的中译英翻译引擎。严格遵守：\n"
    "1. 只输出英文译文本身，不要输出原文、解释或任何前后缀。\n"
    "2. 译文准确、自然、符合英语表达习惯，不要逐字直译。\n"
    "3. 保留数字、公式、代码、链接；专有名词使用通行的英文写法。\n"
    "4. 原文口语则译文口语，原文正式则译文正式。\n"
    "5. 译文中不得出现任何中文字符；原文中的中文词如果没有对应英文，用英文解释它。\n"
    "6. 不要用 Markdown 代码块包裹译文。"
)

SUMMARY_PROMPT = (
    "你是一个专业的内容摘要助手。用户会给你一段文字（中文或英文），"
    "请用简体中文输出摘要，严格按以下纯文本格式：\n"
    "要点：\n"
    "• 要点1\n"
    "• 要点2\n"
    "• 要点3\n"
    "一句话总结：……\n"
    "要求：只根据原文内容，不添加原文没有的信息、不评论；"
    "要点控制在 3~6 条，每条一句话；不要 Markdown 符号、不要表格。"
)

TERM_PROMPT = (
    "你是一个术语解释助手。用户会给你一个术语（可能是词组、缩写或专业名词），"
    "请用简体中文输出解释，严格按以下纯文本格式：\n"
    "中文译名：……\n"
    "所属领域：……\n"
    "释义：……\n"
    "常见用法：……\n"
    "示例：英文示例 —— 中文说明\n"
    "要求：准确精炼；术语确实没有通用中文译名时保留英文并说明；"
    "不确定时说明可能含义，不要编造；不要输出无关内容。"
)


def is_single_word(text: str) -> bool:
    """判断选中的是不是一个单词（用于切换成「单词详解」模式）。"""
    text = (text or "").strip()
    if not text or len(text) > 40:
        return False
    return bool(_SINGLE_WORD.match(text))


def has_cjk(text: str) -> bool:
    """是否含中文（用于自动判断翻译方向）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


class EngineError(RuntimeError):
    pass


class TranslationResult:
    __slots__ = ("text", "seconds", "tokens")

    def __init__(self, text: str, seconds: float, tokens: int):
        self.text = text
        self.seconds = seconds
        self.tokens = tokens

    def __str__(self) -> str:
        return self.text


class Translator:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.exe = config.abspath(cfg["server"]["exe"])
        self.model = config.abspath(cfg["server"]["model"])
        self.host = cfg["server"]["host"]
        self.port = int(cfg["server"]["port"])
        self.base = f"http://{self.host}:{self.port}"
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._translate_lock = threading.Lock()
        self._log_handle = None
        self.ready = False
        # 分片模型只传第一个分片，llama.cpp 会自动加载其余分片
        if "-00001-of-" in self.model and not os.path.exists(self.model):
            base = self.model.split("-00001-of-")[0]
            for name in sorted(os.listdir(os.path.dirname(self.model) or ".")):
                if name.startswith(os.path.basename(base)) and name.endswith(".gguf"):
                    self.model = os.path.join(os.path.dirname(self.model), name)
                    break

    # ---------------- 进程管理 ----------------
    def _health(self, timeout: float = 1.5) -> bool:
        try:
            req = urllib.request.Request(self.base + "/health")
            with _OPENER.open(req, timeout=timeout) as resp:
                body = resp.read(200).decode("utf-8", "ignore")
                return resp.status == 200
        except Exception:
            return False

    def _spawn(self, gpu_layers: int, on_status=None) -> None:
        s = self.cfg["server"]
        if not os.path.exists(self.exe):
            raise EngineError(
                f"找不到推理引擎：{self.exe}\n请先运行 setup.bat 下载运行时与模型。"
            )
        if not os.path.exists(self.model):
            raise EngineError(
                f"找不到模型文件：{self.model}\n请先运行 setup.bat 下载运行时与模型。"
            )
        args = [
            self.exe,
            "-m", self.model,
            "--host", self.host,
            "--port", str(self.port),
            "-c", str(s["context"]),
            "-ngl", str(gpu_layers),
            "-np", "1",
        ]
        if int(s.get("threads") or 0) > 0:
            args += ["-t", str(s["threads"])]

        logs = config.ensure_dirs()
        log_path = os.path.join(logs, "llama-server.log")
        self._log_handle = open(log_path, "ab", buffering=0)
        self._log_handle.write(
            ("\n===== %s start: %s =====\n" % (time.strftime("%F %T"), " ".join(args))).encode("utf-8")
        )
        log.info("启动引擎: %s", " ".join(args))
        if on_status:
            on_status("正在加载本地模型…" if gpu_layers != 0 else "当前无显卡加速，正在用 CPU 加载模型…")
        self.proc = subprocess.Popen(
            args,
            cwd=os.path.dirname(self.exe),
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        deadline = time.time() + float(s.get("startup_timeout", 150))
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise EngineError(
                    f"引擎启动失败（退出码 {self.proc.returncode}），日志：{log_path}"
                )
            if self._health(1.0):
                self.ready = True
                log.info("引擎就绪 (gpu_layers=%s)", gpu_layers)
                return
            time.sleep(0.25)
        raise EngineError(f"引擎加载超时（{s.get('startup_timeout')} 秒），日志：{log_path}")

    def ensure_server(self, on_status=None) -> None:
        """确保 llama-server 在本机运行；必要时拉起（首次会把模型读进显存）。"""
        with self._lock:
            if self.proc is not None and self.proc.poll() is None and self._health(1.0):
                self.ready = True
                return
            if self._health(1.0):
                self.ready = True
                return
            self.stop_locked()
            gpu = int(self.cfg["server"].get("gpu_layers", 99))
            try:
                self._spawn(gpu, on_status)
            except EngineError:
                if gpu == 0:
                    raise
                log.warning("GPU 模式启动失败，回退 CPU 模式")
                self.stop_locked()
                self._spawn(0, on_status)

    def stop_locked(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:
                pass
            self.proc = None
        self.ready = False

    def stop(self) -> None:
        # 用带超时的锁：引擎正在加载模型时（warmup 持锁）不能把退出流程卡死
        acquired = self._lock.acquire(timeout=1.5)
        try:
            self.stop_locked()
        finally:
            if acquired:
                self._lock.release()
        if self._log_handle:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def restart(self) -> None:
        with self._lock:
            self.stop_locked()
        self.ensure_server()

    def warmup(self) -> None:
        """启动时预热：载入模型 + 一次极短翻译，让第一次真正使用也够快。"""
        try:
            self.ensure_server()
            self.translate("Hello.")
            log.info("预热完成")
        except Exception as exc:  # 预热失败不致命，真正翻译时会再报错
            log.warning("预热失败: %s", exc)

    # ---------------- 翻译 ----------------
    def _chunks(self, text: str, limit: int = 1400) -> list[str]:
        text = text.strip()
        if len(text) <= limit:
            return [text]
        parts = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
        chunks, cur = [], ""
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if len(cur) + len(p) + 1 > limit and cur:
                chunks.append(cur)
                cur = p
            else:
                cur = (cur + " " + p).strip()
        if cur:
            chunks.append(cur)
        return chunks

    def translate(self, text: str, on_token=None, on_status=None,
                  direction: str = "en2zh") -> TranslationResult:
        text = (text or "").strip()
        if not text:
            return TranslationResult("", 0.0, 0)
        with self._translate_lock:
            t0 = time.time()
            self.ensure_server(on_status)
            pieces = self._chunks(text)
            out: list[str] = []
            tokens = 0
            for i, chunk in enumerate(pieces):
                if on_status and len(pieces) > 1:
                    on_status(f"正在翻译…（{i + 1}/{len(pieces)} 段）")
                part, n = self._translate_one(chunk, on_token, direction)
                tokens += n
                out.append(part.strip())
            result = "\n\n".join(x for x in out if x).strip()
            if direction == "zh2en" and has_cjk(result):
                # 中译英偶尔会混入中文，做一次纯英文修正（不流式，避免界面重复）
                if on_status:
                    on_status("正在校正英文…")
                fixed, n = self._chat(
                    [
                        {"role": "system",
                         "content": "你是英语母语编辑。用户会给你一段混入中文的英文译文，"
                                    "请只输出修正后的纯英文译文，不得保留任何中文字符，不要解释。"},
                        {"role": "user", "content": f"修正下面的译文：\n\n{result}"},
                    ],
                    max_tokens=800,
                    temperature=0.2,
                )
                tokens += n
                result = fixed.strip() or result
            return TranslationResult(clean_output(result), time.time() - t0, tokens)

    def _translate_one(self, text: str, on_token=None, direction: str = "en2zh"):
        t = self.cfg["translate"]
        if direction == "zh2en":
            system = ZH2EN_PROMPT
            user = f"把下面的中文翻译成英文：\n\n{text}"
        else:
            system = t["system_prompt"]
            user = f"把下面的英文翻译成中文：\n\n{text}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._chat(messages, on_token)

    def explain(self, word: str, on_token=None, on_status=None) -> TranslationResult:
        """单个单词：输出词典式精确讲解（音标/词性/释义/搭配/例句）。"""
        word = (word or "").strip()
        with self._translate_lock:
            t0 = time.time()
            self.ensure_server(on_status)
            messages = [
                {"role": "system", "content": EXPLAIN_PROMPT},
                {"role": "user", "content": f"单词：{word}"},
            ]
            text, n = self._chat(messages, on_token, max_tokens=700, temperature=0.3)
            return TranslationResult(clean_output(text), time.time() - t0, n)

    def explain_term(self, term: str, on_token=None, on_status=None) -> TranslationResult:
        """术语/词组/缩写解释。"""
        term = (term or "").strip()
        with self._translate_lock:
            t0 = time.time()
            self.ensure_server(on_status)
            messages = [
                {"role": "system", "content": TERM_PROMPT},
                {"role": "user", "content": f"术语：{term}"},
            ]
            text, n = self._chat(messages, on_token, max_tokens=600, temperature=0.3)
            return TranslationResult(clean_output(text), time.time() - t0, n)

    def summarize(self, text: str, on_token=None, on_status=None) -> TranslationResult:
        """长文摘要：长文本自动分段摘要后再合并，输出中文要点 + 一句话总结。"""
        text = (text or "").strip()
        if not text:
            return TranslationResult("", 0.0, 0)
        with self._translate_lock:
            t0 = time.time()
            self.ensure_server(on_status)
            chunks = self._chunks(text, limit=1600)
            total_tokens = 0
            if len(chunks) == 1:
                messages = [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": f"请给下面这段内容做摘要：\n\n{text}"},
                ]
                out, n = self._chat(messages, on_token, max_tokens=800, temperature=0.3)
                total_tokens += n
            else:
                partials: list[str] = []
                for i, chunk in enumerate(chunks):
                    if on_status:
                        on_status(f"正在分块摘要…（{i + 1}/{len(chunks)} 段）")
                    messages = [
                        {"role": "system", "content": SUMMARY_PROMPT},
                        {"role": "user", "content": f"请给下面这段内容做摘要：\n\n{chunk}"},
                    ]
                    part, n = self._chat(messages, max_tokens=500, temperature=0.3)
                    total_tokens += n
                    partials.append(part.strip())
                if on_status:
                    on_status("正在合并摘要…")
                joined = "\n\n".join(partials)
                messages = [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user",
                     "content": "下面是一篇长文分段摘要，请合并成一份最终摘要（同样格式，不重复）：\n\n" + joined},
                ]
                out, n = self._chat(messages, on_token, max_tokens=800, temperature=0.3)
                total_tokens += n
            return TranslationResult(clean_output(out), time.time() - t0, total_tokens)

    def _chat(self, messages, on_token=None, max_tokens=None, temperature=None):
        t = self.cfg["translate"]
        payload = {
            "model": "local",
            "messages": messages,
            "temperature": float(
                t["temperature"] if temperature is None else temperature
            ),
            "top_p": float(t["top_p"]),
            "max_tokens": int(max_tokens or t["max_tokens"]),
            "repeat_penalty": 1.05,
            "cache_prompt": True,
            "stream": True,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        buf, ntok = [], 0
        try:
            with _OPENER.open(req, timeout=300) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        obj = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    choice = (obj.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        buf.append(piece)
                        ntok += 1
                        if on_token:
                            on_token(piece)
                    if choice.get("finish_reason"):
                        break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:400]
            raise EngineError(f"引擎返回错误 {exc.code}: {detail}") from exc
        except Exception as exc:
            raise EngineError(f"请求本地引擎失败: {exc}") from exc
        return "".join(buf), ntok


def clean_output(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    text = CLEAN_PREFIX.sub("", text)
    m = CLEAN_WRAP.match(text)
    if m and "\n" not in text:
        text = m.group(1).strip()
    # 去掉偶发的 “(译)” 类结尾标注
    text = re.sub(r"\s*[（(]\s*(译|译文|翻译)\s*[)）]\s*$", "", text)
    return text.strip()
