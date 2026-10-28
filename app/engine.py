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


# 判"词粘在一起"用的：连续 12+ 个字母、中间没有任何空格
_LONG_RUN = re.compile(r"[A-Za-z]{12,}")


def looks_glued(text: str) -> bool:
    """这段英文是不是丢空格了（从 PDF 复制常见：词与词之间没有空格）。

    只在"一眼就该有空格却没有"时才返回 True：字母够多、空格少得离谱、
    而且确实有好几段长到不可能是正常单词的连写。代码、URL、单个长单词
    都会因为不满足这些条件被放过。
    """
    text = (text or "").strip()
    if not text:
        return False
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if letters < 30:
        return False
    # 正常英文的空格约占字母的 15%；低于 8% 就明显不对劲了
    spaces = len(text) - len(text.replace(" ", ""))
    if spaces >= letters * 0.08:
        return False
    # 正常英文里不该出现的"单词"：一串没法当单词看的连写。
    # 有标点时判得宽一点（整句粘连几乎一定带标点）；没标点时只认"整段就是
    # 一串小写字母"——驼峰命名（代码里的长函数名）会被这条排除掉。
    runs = _LONG_RUN.findall(text)
    if re.search(r"[.,;:!?，。；：！？]", text):
        ok = len(runs) >= 2 or any(
            len(r) >= 20 and sum(1 for ch in r if ch.isupper()) <= 2 for r in runs)
    else:
        ok = any(
            len(r) >= 25 and sum(1 for ch in r if ch.isupper()) <= 1 for r in runs)
    if not ok:
        return False
    # 下划线和花括号是代码特征；中文段落也不需要补空格
    if text.count("_") > 1 or "{" in text or "}" in text:
        return False
    if has_cjk(text):
        return False
    return True


def _squeeze(text: str) -> str:
    """去掉所有空白，用来校验"只加了空格、没改内容"。"""
    return "".join((text or "").split())


SPACE_FIX_PROMPT = (
    "你是英文分词修复工具。用户给的英文是从 PDF 复制出来的，单词之间甚至单词内部的"
    "空格全部丢失了，成了一串连写。请把空格准确补回去，让文本恢复成正常英文。规则：\n"
    "1. 只补空格，严禁改动、增删任何字母、数字、标点或大小写；严禁翻译、解释、加引号或换行。\n"
    "2. 数学和学术文本里的单字母变量（如 A、U、b、x、j、k）一律单独成词，"
    "不要和相邻单词粘在一起。\n"
    "3. 数学表达式内部按原样保留，不要为了断词去拆数字和符号，例如 1,...,j 保持原样。\n"
    "示例：\n"
    "输入：EachrowofUisthecorrespondingeigenvectorofA\n"
    "输出：Each row of U is the corresponding eigenvector of A\n"
    "输入：ThematrixAisinvertibleifandonlyifthedeterminantofAisnonzero\n"
    "输出：The matrix A is invertible if and only if the determinant of A is nonzero\n"
    "输入：Letv1,...,vnbeabasisofRn\n"
    "输出：Let v1,...,vn be a basis of R n\n"
    "只输出修复后的文本本身，不要任何说明。"
)


class EngineError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    """任务被用户打断：关掉面板、按 Esc，或者被新任务顶掉。

    单独一个类型而不是复用 EngineError：上层见到它要**静默退出**，
    不弹错误、不写"失败"状态——是用户自己不要这个结果。
    """


class Cancel:
    """一次任务的取消信号，跨线程传递（UI 线程 set，工作线程 check）。

    为什么需要它而不只是"不看结果"：图片识别在 CPU 上要跑十几秒到几分钟，
    用户关掉面板就是想让它停下，而不是换个地方偷偷跑完继续占着 CPU。
    check() 在流式循环里每收到一段就调一次，一旦取消就抛出 Cancelled，
    连接随之关闭，llama-server 那边也就停了。
    """

    __slots__ = ("_event", "reason")

    def __init__(self, reason: str = ""):
        self._event = threading.Event()
        self.reason = reason

    def cancel(self, reason: str = "") -> None:
        if reason:
            self.reason = reason
        self._event.set()

    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled(self.reason or "任务已取消")


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
                  direction: str = "en2zh", cancel: Cancel | None = None) -> TranslationResult:
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
                if cancel is not None:
                    cancel.check()
                if on_status and len(pieces) > 1:
                    on_status(f"正在翻译…（{i + 1}/{len(pieces)} 段）")
                part, n = self._translate_one(chunk, on_token, direction, cancel)
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
                    cancel=cancel,
                )
                tokens += n
                result = fixed.strip() or result
            return TranslationResult(clean_output(result), time.time() - t0, tokens)

    def restore_spaces(self, text: str, on_status=None,
                       cancel: Cancel | None = None) -> str:
        """把丢空格的英文补回空格（只加空格，绝不改内容）。

        模型输出和原文"去掉所有空白后"不完全一致时，宁可原样返回——补空格是
        锦上添花，改坏原文才是事故。所以这一步永远不会让译文变差。
        """
        text = (text or "").strip()
        if not text:
            return text
        # 上下文只有 4096：超长选区塞进去会把译文挤掉，宁可不还原（翻译本身会分段）
        if len(text) > 1800:
            log.info("原文 %d 字符且缺空格，超过还原上限，跳过", len(text))
            return text
        with self._translate_lock:
            self.ensure_server(on_status)
            if on_status:
                on_status("原文缺少空格，正在还原…")
            try:
                out, _ = self._chat(
                    [
                        {"role": "system", "content": SPACE_FIX_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    max_tokens=min(2600, len(text) * 2 + 128),
                    temperature=0.0,
                    cancel=cancel,
                )
            except Cancelled:
                raise
            except EngineError as exc:
                log.warning("还原空格失败，按原文继续：%s", exc)
                return text
        fixed = (out or "").strip().strip('"').strip()
        if not fixed or fixed == text:
            return text
        if "\n" in fixed and "\n" not in text:
            fixed = " ".join(fixed.split())
        if _squeeze(fixed) != _squeeze(text):
            log.warning("还原空格的结果和原文对不上（模型动了内容），已丢弃")
            return text
        log.info("已还原原文空格：%d -> %d 字符", len(text), len(fixed))
        return fixed

    def _translate_one(self, text: str, on_token=None, direction: str = "en2zh",
                       cancel: Cancel | None = None):
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
        return self._chat(messages, on_token, cancel=cancel)

    def explain(self, word: str, on_token=None, on_status=None,
                cancel: Cancel | None = None) -> TranslationResult:
        """单个单词：输出词典式精确讲解（音标/词性/释义/搭配/例句）。"""
        word = (word or "").strip()
        with self._translate_lock:
            t0 = time.time()
            self.ensure_server(on_status)
            messages = [
                {"role": "system", "content": EXPLAIN_PROMPT},
                {"role": "user", "content": f"单词：{word}"},
            ]
            text, n = self._chat(messages, on_token, max_tokens=700,
                                 temperature=0.3, cancel=cancel)
            return TranslationResult(clean_output(text), time.time() - t0, n)

    def explain_term(self, term: str, on_token=None, on_status=None,
                     cancel: Cancel | None = None) -> TranslationResult:
        """术语/词组/缩写解释。"""
        term = (term or "").strip()
        with self._translate_lock:
            t0 = time.time()
            self.ensure_server(on_status)
            messages = [
                {"role": "system", "content": TERM_PROMPT},
                {"role": "user", "content": f"术语：{term}"},
            ]
            text, n = self._chat(messages, on_token, max_tokens=600,
                                 temperature=0.3, cancel=cancel)
            return TranslationResult(clean_output(text), time.time() - t0, n)

    def summarize(self, text: str, on_token=None, on_status=None,
                  cancel: Cancel | None = None) -> TranslationResult:
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
                out, n = self._chat(messages, on_token, max_tokens=800,
                                    temperature=0.3, cancel=cancel)
                total_tokens += n
            else:
                partials: list[str] = []
                for i, chunk in enumerate(chunks):
                    if cancel is not None:
                        cancel.check()
                    if on_status:
                        on_status(f"正在分块摘要…（{i + 1}/{len(chunks)} 段）")
                    messages = [
                        {"role": "system", "content": SUMMARY_PROMPT},
                        {"role": "user", "content": f"请给下面这段内容做摘要：\n\n{chunk}"},
                    ]
                    part, n = self._chat(messages, max_tokens=500,
                                         temperature=0.3, cancel=cancel)
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
                out, n = self._chat(messages, on_token, max_tokens=800,
                                    temperature=0.3, cancel=cancel)
                total_tokens += n
            return TranslationResult(clean_output(out), time.time() - t0, total_tokens)

    def _chat(self, messages, on_token=None, max_tokens=None, temperature=None,
              cancel: Cancel | None = None):
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
                    # 每收到一段就查一次取消：用户已经关掉面板的话，
                    # 这里立刻抛出，with 退出时会关闭连接，服务端随之停止生成
                    if cancel is not None:
                        cancel.check()
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
        except Cancelled:
            # 用户主动中断，不是错误：原样往上抛，别包装成 EngineError
            raise
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
