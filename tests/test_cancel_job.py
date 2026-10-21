"""关掉面板能不能真的把识别停下来（不是"不看结果"，是"别再跑了"）。

四件事：
  1. 取消信号能在流式读的循环里立刻中断请求，而不是等模型把 1600 token 吐完
  2. 面板关闭 → 主程序取消任务、busy 复位（否则用户再截图被 busy 拦住，像卡死）
  3. 旧任务残留的事件（token/done）不能写进新面板
  4. ResultWindow 只在"用户主动关闭"时通知 on_dismiss，重复 close 不重复通知

用法：python tests/test_cancel_job.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.engine import Cancel, Cancelled, Translator  # noqa: E402
from app.main import TranslatorApp  # noqa: E402

TOKENS = 200          # 假服务器准备吐多少段


class _FakeLlama(BaseHTTPRequestHandler):
    """最小可用的假 llama-server：/health 说 OK，/v1/chat/completions 慢慢流。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):  # 别刷屏
        pass

    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for i in range(TOKENS):
                obj = {"choices": [{"delta": {"content": "字"}, "finish_reason": None}]}
                line = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
                self.wfile.write(b"%x\r\n" % len(line) + line + b"\r\n")
                self.wfile.flush()
                time.sleep(0.02)      # 每段 20ms，全跑完要 4 秒
            done = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n" % len(done) + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # 客户端取消后断开是预期行为（Windows 上报的是 ConnectionAborted 10053）
            pass


def start_fake() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def case_cancel_interrupts_stream() -> bool:
    print("\n[1] 取消能不能立刻中断流式请求")
    srv, port = start_fake()
    cfg = config.load_config()
    cfg["server"]["host"] = "127.0.0.1"
    cfg["server"]["port"] = port
    tr = Translator(cfg)

    cancel = Cancel()
    got: list[str] = []
    box: dict = {}

    def run():
        t0 = time.time()
        try:
            tr.translate("hello world", on_token=got.append, cancel=cancel)
            box["ret"] = "正常跑完了（不应该）"
        except Cancelled:
            box["ret"] = "Cancelled"
        box["seconds"] = time.time() - t0

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.5)
    cancel.cancel("测试中断")
    th.join(timeout=5)
    srv.shutdown()

    n = len(got)
    ok = box.get("ret") == "Cancelled" and n < TOKENS // 2
    print(f"    结果={box.get('ret')}  耗时={box.get('seconds', -1):.2f}s  "
          f"收到 {n}/{TOKENS} 段")
    print(f"    {'✓ 提前退出' if ok else '✗ 没能在取消点退出（或跑完了全部）'}")
    return ok


def case_dismiss_resets_busy() -> bool:
    print("\n[2] 关面板 → busy 复位 + 取消信号发出")
    cancel = Cancel()
    fake = types.SimpleNamespace(
        busy=True, _epoch=7, _cancel=cancel, _tray_title=lambda _t: None
    )
    TranslatorApp._on_panel_dismiss(fake)
    ok = fake.busy is False and cancel.cancelled() and fake._epoch == 8
    print(f"    busy={fake.busy} 已取消={cancel.cancelled()} epoch={fake._epoch}")
    print(f"    {'✓' if ok else '✗'}")
    return ok


def case_stale_events_dropped() -> bool:
    print("\n[3] 旧任务的事件不能写进新面板")
    calls: list[str] = []
    fake = types.SimpleNamespace(
        _epoch=9,
        result=types.SimpleNamespace(
            append=lambda s: calls.append("append:" + s),
            set_status=lambda s: calls.append("status:" + s),
            set_final=lambda s: calls.append("final:" + s),
            finish=lambda *a: calls.append("finish"),
            _copy_all=lambda: calls.append("copy"),
            show_error=lambda s: calls.append("error:" + s),
        ),
        cfg={"ui": {}},
        _tray_title=lambda _t: None,
    )
    TranslatorApp._handle(fake, ("token", "旧", 8))     # 过期
    TranslatorApp._handle(fake, ("status", "旧", 8))    # 过期
    TranslatorApp._handle(fake, ("done", types.SimpleNamespace(
        text="旧结果", seconds=1.0, tokens=1), "image", 8))
    dropped = not calls

    TranslatorApp._handle(fake, ("token", "新", 9))     # 当前
    kept = calls == ["append:新"]
    print(f"    过期事件全部丢弃={dropped}  当前事件照常处理={kept}")
    print(f"    {'✓' if dropped and kept else '✗'}  收到 {calls}")
    return dropped and kept


def case_panel_dismiss_once() -> bool:
    import tkinter as tk

    from app.ui import ResultWindow

    print("\n[4] ResultWindow 只在用户主动关闭时通知一次")
    root = tk.Tk()
    root.withdraw()
    hits: list[int] = []
    panel = ResultWindow(root, config.load_config(),
                         on_dismiss=lambda: hits.append(1))
    panel.open(0, 0, "测试", "正在识别…")
    root.update()
    panel.close()
    root.update()
    first = len(hits)
    panel.close()          # 重复关闭（Esc + 巡检都可能再调一次）
    root.update()
    second = len(hits)
    # 从没打开过就 close，不该通知
    panel2 = ResultWindow(root, config.load_config(),
                          on_dismiss=lambda: hits.append(1))
    panel2.close()
    root.update()
    third = len(hits)
    root.destroy()

    ok = first == 1 and second == 1 and third == 1
    print(f"    关闭一次通知 {first} 次，再关一次累计 {second} 次，"
          f"未打开就关累计 {third} 次")
    print(f"    {'✓' if ok else '✗'}")
    return ok


def case_image_job_cancelled() -> bool:
    """完整链路：translate_image → vision → engine → 面板关闭 → 线程退出。

    前三段单测分别验了「信号能中断流」「关面板会发信号」「过期事件被丢弃」，
    这里把它们接起来跑一遍：真实地起线程、真实地读一个慢流、中途取消，
    确认后台真的停了（token 不再增长）而且没有 done 事件漏出来。
    """
    print("\n[5] 图片翻译中途关面板：后台真的停、结果不外漏")
    import queue

    from PIL import Image

    from app.vision import VisionTranslator

    srv, port = start_fake()
    cfg = config.load_config()
    cfg["vision"]["host"] = "127.0.0.1"
    cfg["vision"]["port"] = port

    q: queue.Queue = queue.Queue()
    opened: list[str] = []
    app = types.SimpleNamespace(
        q=q, busy=False, _epoch=0, _cancel=None,
        vision=VisionTranslator(cfg), mode="translate", pending=("", 0, 0),
        cfg=cfg, _tray_title=lambda _t: None,
        result=types.SimpleNamespace(
            set_style=lambda s: None,
            open=lambda *a: opened.append(a[2]),
        ),
    )
    app._begin_job = lambda: TranslatorApp._begin_job(app)
    app._worker_image = lambda *a: TranslatorApp._worker_image(app, *a)

    img = Image.new("RGB", (240, 120), "white")
    TranslatorApp.translate_image(app, img, "测试图")
    time.sleep(0.6)

    def drain() -> tuple[int, int]:
        tokens = done = 0
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "token":
                tokens += 1
            elif item[0] == "done":
                done += 1
        return tokens, done

    before, _ = drain()
    TranslatorApp._on_panel_dismiss(app)      # 点叉时 close() 回调的就是它
    time.sleep(0.8)
    mid, done_mid = drain()
    time.sleep(1.0)
    after, done_after = drain()
    srv.shutdown()

    stopped = after == 0                      # 取消后一段都不该再来
    ok = (opened == ["测试图"] and app.busy is False
          and before > 0 and stopped and done_mid + done_after == 0)
    print(f"    取消前收到 {before} 段；取消后 0.8s 内 {mid} 段、再过 1s {after} 段；"
          f"done 事件 {done_mid + done_after} 个")
    print(f"    busy={app.busy}（应为 False），面板已开过={opened}")
    print(f"    {'✓' if ok else '✗'}")
    return ok


def main() -> int:
    results = [
        case_cancel_interrupts_stream(),
        case_dismiss_resets_busy(),
        case_stale_events_dropped(),
        case_panel_dismiss_once(),
        case_image_job_cancelled(),
    ]
    ok = all(results)
    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
