"""图片识别引擎：另外起一个 llama-server 专门带视觉（mmproj），纯 CPU 跑。

为什么不跟文字引擎合在一起：
  · 8G 显存装不下 7B 文字模型 + VL 模型两套权重，同时驻留会溢出到内存，反而更慢；
  · 实测 VL-4B 纯 CPU 识别一张图 13~15 秒，对"拍张 PPT 让它翻"完全够用；
  · 独立进程还能按需启动、闲置自动退出，不用时可以一点内存都不占。

对外只有 VisionTranslator.translate_image()：给一张 PIL 图和模式，吐译文。
"""
from __future__ import annotations

import base64
import io
import logging
import os
import subprocess
import threading
import time
import urllib.request

from . import config
from .engine import CLEAN_PREFIX, CLEAN_WRAP, CREATE_NO_WINDOW, EngineError, TranslationResult, _OPENER

log = logging.getLogger("vision")


class PortBindError(EngineError):
    """端口绑定失败：可以让上层换个端口重试，而不是直接报错给用户。"""

# 图片翻译的提示词。这里比文字翻译严厉得多：图片里的内容是模型"看"出来的，
# 一旦它顺手补一句原文没有的话，用户没有任何办法察觉。
IMAGE_PROMPT = (
    "你是一个严谨的图片翻译助手。用户会给你一张图片（可能是论文、PPT、扫描件或截图）。"
    "请把图片里的文字内容翻译成简体中文。严格遵守：\n"
    "1. 只翻译图片里真实存在的文字，绝对不许补充、推测、总结或编造图片里没有的内容；\n"
    "2. 所有数学公式一律用 LaTeX 表示，行内公式用 $...$，独立公式用 $$...$$；"
    "严格保留上标、下标、求和上下限、分式、根号、矩阵、希腊字母，数字和指数一个都不许改；\n"
    "3. 公式本身不翻译（变量名、函数名保持原样），只翻译公式周围的自然语言；\n"
    "4. 按原文的层次和段落顺序输出，标题、项目符号、编号都要保留；\n"
    "5. 某个词或公式实在看不清时，写 [未识别] 并跳过，不要猜；\n"
    "6. 不要输出「以下是翻译」「图片中写着」这类前后缀，直接给译文；\n"
    "7. 如果图片里全是中文，就原样输出原文（公式仍转成 LaTeX）。"
)

IMAGE_SUMMARY_PROMPT = (
    "你是一个严谨的内容摘要助手。用户会给你一张图片（论文、幻灯片或扫描件）。"
    "请用简体中文输出摘要，严格按以下纯文本格式：\n"
    "要点：\n• 要点1\n• 要点2\n• 要点3\n一句话总结：……\n"
    "要求：只根据图片里真实存在的内容，不补充、不推测；要点 3~6 条；"
    "公式用 LaTeX 表示；某个地方看不清就跳过，不要编。"
)

IMAGE_TERM_PROMPT = (
    "你是一个术语解释助手。用户会给你一张图片，图片里通常有几个专业术语或缩写。"
    "请逐个用简体中文解释，每个术语按以下格式：\n"
    "术语：……\n中文译名：……\n释义：……\n"
    "要求：只解释图片里真实出现的术语；准确精炼；不确定时说明可能含义，不要编造。"
)


def encode_image(img, max_side: int = 1600, quality: int = 92) -> tuple[str, tuple[int, int]]:
    """PIL 图 → data URL 里的 base64。过大时等比缩小：CPU 推理的耗时几乎正比于像素数。"""
    from PIL import Image

    if not isinstance(img, Image.Image):
        raise EngineError("不是有效的图片对象")
    # 视觉模型只吃三通道；带透明通道的截图直接转 RGB 会得到黑底，先铺白底
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        img = canvas
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    longest = max(w, h)
    if max_side and longest > max_side:
        ratio = max_side / float(longest)
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
        log.info("图片过大，已缩放到 %dx%d（原始 %dx%d）", img.width, img.height, w, h)

    buf = io.BytesIO()
    # 照片用 JPEG 更省带宽/内存；带文字和细线的截图用 PNG 更清楚
    if quality and len(img.getcolors(maxcolors=1 << 20) or []) > 2000:
        img.save(buf, format="JPEG", quality=quality)
        mime = "image/jpeg"
    else:
        img.save(buf, format="PNG")
        mime = "image/png"
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:{mime};base64,{b64}", (img.width, img.height)


class VisionTranslator:
    """带视觉能力的 llama-server 子进程管理器 + 图片翻译入口。"""

    def __init__(self, cfg: dict):
        v = cfg.get("vision", {}) or {}
        self.cfg = cfg
        self.exe = config.abspath(v.get("exe", r"runtime\llama\llama-server.exe"))
        self.model = config.abspath(v.get("model", ""))
        self.mmproj = config.abspath(v.get("mmproj", ""))
        self.host = v.get("host", "127.0.0.1")
        self.port = int(v.get("port", 8378))
        self.base = f"http://{self.host}:{self.port}"
        self.context = int(v.get("context", 8192))
        self.gpu_layers = int(v.get("gpu_layers", 0))
        self.threads = int(v.get("threads", 0))
        self.startup_timeout = float(v.get("startup_timeout", 120))
        self.idle_timeout = float(v.get("idle_timeout", 600))
        self.max_image_side = int(v.get("max_image_side", 1600))

        self.proc: subprocess.Popen | None = None
        self.ready = False
        self._lock = threading.Lock()          # 串行化启动/退出
        self._request_lock = threading.Lock()  # llama-server -np 1，一次只能处理一个请求
        self._last_used = 0.0
        self._log_handle = None
        self._idle_started = False
        self._idle_stop = False
        self._log_path = os.path.join(config.ROOT, "logs", "vision-server.log")

    # ---------------- 可用性 ----------------
    def available(self) -> bool:
        return os.path.exists(self.exe) and os.path.exists(self.model) and os.path.exists(self.mmproj)

    def missing_files(self) -> list[str]:
        return [p for p in (self.exe, self.model, self.mmproj) if not os.path.exists(p)]

    def hint(self) -> str:
        return (
            "图片翻译需要视觉模型（约 3.2GB），当前缺少：\n  "
            + "\n  ".join(self.missing_files())
            + "\n请运行 setup.bat 下载，或把模型放到 models\\vision\\ 目录下。"
        )

    # ---------------- 进程管理 ----------------
    def _health(self, timeout: float = 1.5) -> bool:
        try:
            req = urllib.request.Request(self.base + "/health")
            with _OPENER.open(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _set_port(self, port: int) -> None:
        self.port = int(port)
        self.base = f"http://{self.host}:{self.port}"

    @staticmethod
    def _port_free(host: str, port: int) -> bool:
        """先自己试绑一下：被别的程序占用时直接跳过，省得白等一次引擎启动。"""
        import socket

        sock = socket.socket()
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _port_candidates(self) -> list[int]:
        """首选配置端口，往后顺延几个。端口被占/被系统拦下时自动换，不让功能直接失效。"""
        base = int(self.cfg.get("vision", {}).get("port", 8378))
        out = []
        for delta in (0, 1, 2, 4, 6):
            port = base + delta
            if port not in out and self._port_free(self.host, port):
                out.append(port)
        return out or [base]

    def _last_log_tail(self, lines: int = 8) -> str:
        try:
            with open(self._log_path, "r", encoding="utf-8", errors="ignore") as f:
                return "".join(f.readlines()[-lines:])
        except Exception:
            return ""

    def _spawn(self, on_status=None) -> None:
        v = self.cfg.get("vision", {})
        args = [
            self.exe,
            "-m", self.model,
            "--mmproj", self.mmproj,
            "--host", self.host,
            "--port", str(self.port),
            "-c", str(self.context),
            "-ngl", str(self.gpu_layers),
            "-np", "1",
            # Qwen-VL 官方建议的视觉 token 下限。给小了密集排版的小字会糊成一团。
            "--image-min-tokens", str(int(v.get("min_image_tokens", 1024))),
            "--jinja",
            "--no-warmup",
        ]
        if self.threads > 0:
            args += ["-t", str(self.threads)]

        logs = config.ensure_dirs()
        self._log_path = os.path.join(logs, "vision-server.log")
        self._log_handle = open(self._log_path, "ab", buffering=0)
        self._log_handle.write(
            ("\n===== %s start: %s =====\n" % (time.strftime("%F %T"), " ".join(args))).encode("utf-8")
        )
        log.info("启动视觉引擎: %s", " ".join(args))
        if on_status:
            on_status("正在启动图片识别引擎（首次约 10 秒）…")
        self.proc = subprocess.Popen(
            args,
            cwd=os.path.dirname(self.exe),
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                rc = self.proc.returncode
                tail = self._last_log_tail()
                if "couldn't bind" in tail or "bind HTTP server socket" in tail:
                    # 端口被抢/被系统拦住：换一个端口重试，别让整个功能失效
                    raise PortBindError(f"端口 {self.port} 无法绑定")
                raise EngineError(
                    f"图片识别引擎启动失败（退出码 {rc}），日志：{self._log_path}"
                )
            if self._health(1.0):
                self.ready = True
                log.info("视觉引擎就绪 (port=%d)", self.port)
                return
            time.sleep(0.25)
        raise EngineError(
            f"图片识别引擎加载超时（{self.startup_timeout:.0f} 秒），日志：{self._log_path}"
        )

    def ensure_server(self, on_status=None) -> None:
        if not self.available():
            raise EngineError(self.hint())
        with self._lock:
            if self.proc is not None and self.proc.poll() is None and self._health(1.0):
                self.ready = True
                self._last_used = time.time()
                return
            if self._health(1.0):
                self.ready = True
                self._last_used = time.time()
                return
            self._stop_locked()

            last_exc: Exception | None = None
            for port in self._port_candidates():
                self._set_port(port)
                try:
                    self._spawn(on_status)
                    self._last_used = time.time()
                    self._start_idle_watch()
                    return
                except PortBindError as exc:
                    log.warning("视觉引擎端口 %d 不可用，尝试下一个", port)
                    last_exc = exc
                    self._stop_locked()
                except EngineError:
                    self._stop_locked()
                    raise
            raise EngineError(
                f"图片识别引擎无法监听本机端口（试过 {self._port_candidates()}）。"
                f"可能是防火墙或端口保留导致，日志：{self._log_path}"
            ) from last_exc

    def _stop_locked(self) -> None:
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
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def stop(self) -> None:
        self._idle_stop = True
        acquired = self._lock.acquire(timeout=2.0)
        try:
            self._stop_locked()
        finally:
            if acquired:
                self._lock.release()

    def _start_idle_watch(self) -> None:
        """闲置一段时间自动退出，别白占 3.2GB 内存。"""
        if self._idle_started or self.idle_timeout <= 0:
            return
        self._idle_started = True

        def run() -> None:
            while not self._idle_stop:
                time.sleep(30)
                if not self.ready or self.idle_timeout <= 0:
                    continue
                if time.time() - self._last_used < self.idle_timeout:
                    continue
                # 正在处理请求时不能杀：忙就再等一轮
                if not self._request_lock.acquire(blocking=False):
                    continue
                try:
                    with self._lock:
                        if self.proc is None:
                            break
                        log.info("视觉引擎闲置 %.0f 秒，退出以释放内存", time.time() - self._last_used)
                        self._stop_locked()
                finally:
                    self._request_lock.release()
                break

        threading.Thread(target=run, daemon=True).start()

    # ---------------- 请求 ----------------
    def _chat_image(self, data_url: str, system: str, user: str,
                    on_token=None, max_tokens: int = 1400) -> tuple[str, int]:
        payload_messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        t = self.cfg["translate"]
        import json

        payload = {
            "model": "local",
            "messages": payload_messages,
            "temperature": 0.1,          # 认字要稳，不要发挥
            "top_p": 0.9,
            "max_tokens": int(max_tokens),
            "repeat_penalty": 1.05,
            "stream": True,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        buf: list[str] = []
        ntok = 0
        try:
            # CPU 推理慢，超时要给足；单张扫描件 15 秒左右，长图可能到两三分钟
            with _OPENER.open(req, timeout=600) as resp:
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
                    piece = (choice.get("delta") or {}).get("content") or ""
                    if piece:
                        buf.append(piece)
                        ntok += 1
                        if on_token:
                            on_token(piece)
                    if choice.get("finish_reason"):
                        break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:400]
            raise EngineError(f"图片识别失败（{exc.code}）：{detail}") from exc
        except Exception as exc:
            raise EngineError(f"请求图片识别引擎失败：{exc}") from exc
        return "".join(buf), ntok

    def translate_image(self, img, mode: str = "translate", on_token=None,
                        on_status=None, max_side: int | None = None) -> TranslationResult:
        """把一张图（PIL.Image 或图片路径）翻成中文，公式保留 LaTeX。"""
        from PIL import Image

        if isinstance(img, (str, bytes, os.PathLike)):
            src = Image.open(img) if not isinstance(img, bytes) else Image.open(io.BytesIO(img))
            src.load()
        else:
            src = img

        with self._request_lock:
            t0 = time.time()
            self.ensure_server(on_status)
            self._last_used = time.time()
            if on_status:
                on_status("正在识别图片内容（纯 CPU，约 10~30 秒）…")
            data_url, size = encode_image(src, max_side or self.max_image_side)
            log.info("图片识别开始：%dx%d, base64 %.1f MB", size[0], size[1], len(data_url) / 1e6)

            if mode == "summary":
                system, user = IMAGE_SUMMARY_PROMPT, "请给这张图片里的内容做摘要。"
            elif mode == "term":
                system, user = IMAGE_TERM_PROMPT, "请解释这张图片里出现的专业术语。"
            else:
                system, user = IMAGE_PROMPT, "请翻译这张图片里的内容。"

            text, ntok = self._chat_image(
                data_url, system, user, on_token=on_token,
                max_tokens=1600 if mode != "summary" else 900,
            )
            self._last_used = time.time()
            return TranslationResult(clean_image_output(text), time.time() - t0, ntok)


# 图片翻译特有：模型很喜欢先来一句 "以下是图片的翻译："，再包一层 ```latex
_IMG_PREFIX = (
    "以下是图片内容的翻译：|以下是图片的翻译：|以下是翻译：|图片内容翻译如下：|"
    "图片中的内容翻译如下：|翻译如下：|以下是图片中文字的中文翻译："
)


def clean_image_output(text: str) -> str:
    import re

    text = (text or "").strip()
    # 顺序很重要：必须先剥 ```latex 围栏，再剥零散反引号。
    # 反过来会把围栏开头的反引号先吃掉，剩下的 "latex" 就永远匹配不上了。
    text = re.sub(r"^```[a-zA-Z]*[ \t]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip().strip("`").strip()
    text = CLEAN_PREFIX.sub("", text)
    text = re.sub(r"^(?:" + _IMG_PREFIX + r")\s*", "", text).strip()
    m = CLEAN_WRAP.match(text)
    if m and "\n" not in text:
        text = m.group(1).strip()
    return text.strip()
