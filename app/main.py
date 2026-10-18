"""英译中划词翻译助手 —— 程序入口。

功能：
  · 鼠标选中英文并松开 → 光标旁浮出「译」小图标
  · 点击图标 → 本地 7B 大模型翻译成中文，弹出译文卡片
  · 或者选中后按 Ctrl+Alt+T 直接翻译
  · 托盘常驻，可暂停划词 / 开机自启 / 退出

全程本地推理（llama.cpp + Qwen2.5-7B），不联网、不需要任何 API Key。
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from logging.handlers import RotatingFileHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app.engine import EngineError, Translator, has_cjk, is_single_word  # noqa: E402
from app.selection import SelectionWatcher  # noqa: E402
from app.ui import DockButton, FloatingIcon, MascotWindow, ModePopup, ResultWindow  # noqa: E402

log = logging.getLogger("app")

MODE_CHOICES = ("auto", "translate", "zh2en", "summary", "term")
BUILD = "2026-10-18.2030"  # 启动日志里会打印，用于确认跑的是新代码

MODE_LABELS = {
    "auto": "自动识别",
    "translate": "英译中",
    "zh2en": "中译英",
    "summary": "长文摘要",
    "term": "术语解释",
}
MODE_PENDING = {
    "translate": "正在翻译…",
    "zh2en": "正在译成英文…",
    "summary": "正在摘要…",
    "term": "正在解释术语…",
    "word": "正在查词…",
}
MODE_DONE = {
    "translate": "本地模型",
    "zh2en": "中译英",
    "summary": "长文摘要",
    "term": "术语解释",
    "word": "单词详解",
}

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "EnglishChineseTranslator"
MUTEX_NAME = "Local\\EnglishChineseTranslator_v1"
ERROR_ALREADY_EXISTS = 183
PID_FILE = os.path.join(ROOT, "logs", "app.pid")


def write_pid_file() -> None:
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def remove_pid_file() -> None:
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------- 基础工具
def setup_logging(debug: bool = False) -> None:
    logs_dir = config.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    if not root.handlers:
        fh = RotatingFileHandler(
            os.path.join(logs_dir, "app.log"), maxBytes=1_000_000, backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if debug:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def acquire_single_instance():
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def pythonw_path() -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            return candidate
    return exe


def set_autostart(flag: bool) -> None:
    import winreg

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        if flag:
            cmd = f'"{pythonw_path()}" "{os.path.join(ROOT, "app", "main.py")}"'
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    finally:
        key.Close()


# ---------------------------------------------------------------- 主程序
class TranslatorApp:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.q: queue.Queue = queue.Queue()
        self.translator = Translator(cfg)
        self.pending: tuple[str, int, int] = ("", 0, 0)
        self.busy = False
        self.paused = bool(cfg.get("pause", False))
        self._shutting_down = False
        self._mutex = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("英译中助手")
        # Tk 回调里的异常默认只打印到 stderr（pythonw 下看不到），这里写进日志
        self.root.report_callback_exception = self._on_tk_exception
        self.icon = FloatingIcon(self.root, cfg, lambda: self.q.put(("icon_click",)))
        panel_gif = None
        if cfg["ui"].get("panel_bg", True):
            candidate = config.abspath(cfg["ui"].get("mascot_gif", r"assets\nahida_swing.gif"))
            if os.path.exists(candidate):
                panel_gif = candidate
        self.result = ResultWindow(
            self.root, cfg, on_retry=self.retry, on_close=self._save_window_pos,
            on_mode_click=lambda: self.q.put(("panel_mode_menu",)),
            bg_gif=panel_gif,
        )
        self.dock = DockButton(
            self.root, cfg,
            lambda: self.q.put(("dock_click",)),
            on_menu=lambda: self.q.put(("dock_menu",)),
        )
        self.watcher = SelectionWatcher(
            on_selection=self._on_selection,
            should_ignore=self._should_ignore,
            on_press=self._on_any_press,
            hotkey=cfg.get("hotkey", ""),
            on_scroll=self._on_scroll,
            on_escape=self._on_escape,
        )
        self.watcher.enabled = not self.paused
        self.tray = None
        self._pause_var = tk.BooleanVar(master=self.root, value=self.paused)
        self._dock_menu: tk.Menu | None = None
        self.mode = str(cfg.get("mode", "auto") or "auto")
        if self.mode not in MODE_CHOICES:
            self.mode = "auto"
        self._mode_var = tk.StringVar(master=self.root, value=self.mode)
        self.mode_popup = ModePopup(
            self.root,
            cfg["ui"].get("font_family", "Microsoft YaHei UI"),
            [(key, MODE_LABELS[key]) for key in MODE_CHOICES],
            self.mode,
            self._set_mode,
        )
        # 看板娘（循环动图）
        self.mascot = None
        self._mascot_var = tk.BooleanVar(
            master=self.root, value=bool(cfg["ui"].get("mascot_enabled", True))
        )
        self._auto_pt_var = tk.BooleanVar(
            master=self.root, value=bool(cfg["ui"].get("panel_auto_passthrough", True))
        )
        gif_path = config.abspath(cfg["ui"].get("mascot_gif", r"assets\nahida_swing.gif"))
        if os.path.exists(gif_path):
            self.mascot = MascotWindow(
                self.root, cfg, gif_path,
                on_click=lambda: self.q.put(("show_window",)),
                on_menu=lambda: self.q.put(("mascot_menu",)),
            )
        else:
            log.info("看板娘动图不存在：%s", gif_path)

    # ---------------- 事件入口（钩子线程 → 队列） ----------------
    def _on_selection(self, text: str, x: int, y: int, auto: bool = False) -> None:
        self.q.put(("selection", text, x, y, auto))

    def _on_any_press(self, x: int, y: int) -> None:
        """钩子线程里只能用不碰 Tk 的快速判断（否则线程会被 Windows 判超时并移除钩子）。"""
        if self.mode_popup.fast_visible() and not self.mode_popup.fast_contains(x, y):
            self.q.put(("hide_mode_popup",))
        if (self.icon.fast_contains(x, y)
                or self._panel_hit(x, y)
                or self.dock.fast_contains(x, y)
                or (self.mascot is not None and self.mascot.fast_contains(x, y))):
            return
        if self.icon.fast_visible():
            self.q.put(("hide_icon",))

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """滚轮滚动 = 用户在干别的事，收起悬浮图标（翻译面板不动）。"""
        if self.icon.fast_visible():
            self.q.put(("hide_icon",))

    def _on_escape(self) -> None:
        self.q.put(("escape",))

    def _panel_hit(self, x: int, y: int) -> bool:
        """面板是否在这个坐标上“真的挡住”鼠标（穿透状态下不算）。"""
        return self.result.fast_contains(x, y) and not self.result.click_through()

    def _should_ignore(self, x: int, y: int) -> bool:
        return (
            self.icon.fast_contains(x, y)
            or self._panel_hit(x, y)
            or self.dock.fast_contains(x, y)
            or (self.mascot is not None and self.mascot.fast_contains(x, y))
        )

    # ---------------- 翻译面板 ----------------
    def _ready_status(self) -> str:
        if self.mode == "auto":
            return "就绪 · 自动识别 · 选中后点「译」或按 Ctrl+Alt+T"
        return f"就绪 · 模式：{MODE_LABELS.get(self.mode, self.mode)} · 选中后点「译」或按 Ctrl+Alt+T"

    def _set_mode(self, mode: str) -> None:
        """切换工作模式（自动 / 英译中 / 中译英 / 长文摘要 / 术语解释）。"""
        if mode not in MODE_CHOICES:
            return
        self.mode = mode
        self.cfg["mode"] = mode
        try:
            self._mode_var.set(mode)
        except Exception:
            pass
        try:
            self.mode_popup.current = mode
        except Exception:
            pass
        try:
            config.save_config(self.cfg)
        except Exception:
            pass
        try:
            self.result.set_mode(mode)
        except Exception:
            pass
        if self.result.visible():
            self.result.set_status(f"已切换模式：{MODE_LABELS.get(mode, mode)}")
        log.info("模式切换为 %s", mode)
        try:
            if self.tray is not None:
                self.tray.update_menu()
        except Exception:
            pass

    def _show_window(self, first: bool = False) -> None:
        """显示（或重新叫出）翻译面板；没有内容时显示空面板。

        first=True 表示程序刚启动：固定出现在屏幕右侧；
        其余情况（点「译」、托盘打开）沿用上次记住的位置。
        """
        if self.result.visible():
            self.result.lift()
            return
        pos = None if first else self.cfg.get("ui", {}).get("window_pos")
        self.result.show_empty(pos=tuple(pos) if pos else None)
        self.result.set_mode(self.mode)
        self.result.set_status(self._ready_status())

    def _save_window_pos(self) -> None:
        try:
            x, y = self.result.position
            ui = self.cfg.setdefault("ui", {})
            ui["window_pos"] = [int(x), int(y)]
            ui["popup_width"] = int(self.result.width)
            ui["popup_height"] = int(self.result.min_h)
            config.save_config(self.cfg)
        except Exception:
            pass

    @staticmethod
    def _mode_menu_anchor(btn) -> tuple[int, int]:
        if btn is not None:
            return btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height() + 6
        return 0, 0

    def _show_panel_mode_menu(self) -> None:
        """翻译面板左上角「模式」按钮：点开自绘的模式浮层。"""
        x, y = self._mode_menu_anchor(self.result.mode_label)
        self.mode_popup.show(x, y, current=self.mode)

    def _show_dock_mode_menu(self) -> None:
        x, y = self.root.winfo_pointerxy()
        self.mode_popup.show(x + 4, y + 6, current=self.mode)

    def _toggle_mascot(self, show: bool | None = None) -> None:
        if self.mascot is None:
            return
        enabled = (not self._mascot_var.get()) if show is None else bool(show)
        self._mascot_var.set(enabled)
        self.cfg["ui"]["mascot_enabled"] = enabled
        try:
            config.save_config(self.cfg)
        except Exception:
            pass
        if enabled:
            self.mascot.show()
        else:
            self.mascot.hide()

    def _show_mascot_menu(self) -> None:
        """看板娘右键菜单。"""
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="打开翻译面板", command=lambda: self.q.put(("show_window",)))
        menu.add_checkbutton(
            label="显示看板娘", variable=self._mascot_var, command=self._toggle_mascot
        )
        menu.add_separator()
        menu.add_command(label="退出程序", command=lambda: self.q.put(("quit",)))
        try:
            x, y = self.root.winfo_pointerxy()
            menu.tk_popup(x, y)
        except tk.TclError:
            pass
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    def _toggle_click_through(self) -> None:
        enabled = not bool(self.cfg["ui"].get("panel_click_through", False))
        self.cfg["ui"]["panel_click_through"] = enabled
        try:
            config.save_config(self.cfg)
        except Exception:
            pass
        self.result.apply_click_through(enabled)
        log.info("面板始终穿透: %s", enabled)

    def _toggle_auto_passthrough(self) -> None:
        enabled = bool(self._auto_pt_var.get())
        self.cfg["ui"]["panel_auto_passthrough"] = enabled
        try:
            config.save_config(self.cfg)
        except Exception:
            pass
        log.info("面板闲置自动穿透: %s", enabled)

    def _show_dock_menu(self) -> None:
        """右下角常驻按钮的右键菜单（模式 / 显示面板 / 退出等）。"""
        if self._dock_menu is None:
            menu = tk.Menu(self.root, tearoff=0)
            menu.add_command(label="模式…", command=lambda: self.q.put(("dock_mode_popup",)))
            menu.add_command(label="显示翻译面板", command=lambda: self.q.put(("show_window",)))
            menu.add_checkbutton(
                label="显示看板娘", variable=self._mascot_var, command=self._toggle_mascot
            )
            menu.add_checkbutton(
                label="面板闲置自动穿透", variable=self._auto_pt_var,
                command=self._toggle_auto_passthrough,
            )
            menu.add_checkbutton(
                label="面板始终鼠标穿透", command=self._toggle_click_through
            )
            menu.add_checkbutton(
                label="暂停划词", variable=self._pause_var, command=self._toggle_pause
            )
            menu.add_command(label="重启本地引擎", command=self._restart_engine)
            menu.add_separator()
            menu.add_command(label="退出程序", command=lambda: self.q.put(("quit",)))
            self._dock_menu = menu
        self._pause_var.set(self.paused)
        try:
            x, y = self.root.winfo_pointerxy()
            self._dock_menu.tk_popup(x, y)
        except tk.TclError:
            pass
        finally:
            try:
                self._dock_menu.grab_release()
            except tk.TclError:
                pass

    def _card_has_focus(self) -> bool:
        """Esc 只在焦点确实位于翻译面板里时才关闭它，避免误关。"""
        try:
            widget = self.root.focus_displayof()
        except tk.TclError:
            return False
        while widget is not None:
            if widget is self.result.win:
                return True
            widget = getattr(widget, "master", None)
        return False

    # ---------------- 队列消费（Tk 主线程） ----------------
    def _on_tk_exception(self, exc_type, exc_value, exc_tb) -> None:
        log.error("Tk 回调异常: %s", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    def _drain(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                # 单个事件出错不能拖垮整个事件循环（否则划词/按钮全部失效）
                try:
                    self._handle(item)
                except Exception:
                    log.exception("处理事件失败: %s", item[0] if item else "?")
        except queue.Empty:
            pass
        if not self._shutting_down:
            self.root.after(30, self._drain)

    def _handle(self, item: tuple) -> None:
        kind = item[0]
        if kind == "selection":
            self.pending = (item[1], item[2], item[3])
            if item[4]:  # 快捷键：直接翻译
                self.icon.hide()
                self.translate_pending()
            else:  # 鼠标划词：先浮出小图标
                log.info("显示划词按钮: %d 字符 @(%s,%s)", len(item[1]), item[2], item[3])
                self.icon.show(item[2], item[3])
        elif kind == "icon_click":
            self.translate_pending()
        elif kind in ("outside_click", "hide_icon"):
            self.icon.hide()
        elif kind == "show_window":
            self._show_window()
        elif kind == "dock_click":
            log.info("点击常驻按钮：面板可见=%s", self.result.visible())
            if self.result.visible():
                self.result._raise_self()
            else:
                self._show_window()
        elif kind == "panel_mode_menu":
            self._show_panel_mode_menu()
        elif kind == "dock_mode_popup":
            self._show_dock_mode_menu()
        elif kind == "hide_mode_popup":
            self.mode_popup.close()
        elif kind == "mascot_menu":
            self._show_mascot_menu()
        elif kind == "dock_menu":
            self._show_dock_menu()
        elif kind == "set_mode":
            self._set_mode(item[1])
        elif kind == "escape":
            self.mode_popup.close()
            self.icon.hide()
            if self._card_has_focus():
                self.result.close()
        elif kind == "status":
            self.result.set_status(item[1])
        elif kind == "token":
            self.result.append(item[1])
        elif kind == "done":
            self.busy = False
            result = item[1]
            mode = item[2] if len(item) > 2 else "translate"
            self.result.set_final(result.text)
            self.result.finish(result.seconds, result.tokens)
            self.result.set_status(
                f"{MODE_DONE.get(mode, '本地模型')} · {result.seconds:.1f}s"
            )
            if self.cfg["ui"].get("copy_on_finish"):
                self.result._copy_all()
            self._tray_title("英译中助手 · 就绪")
        elif kind == "error":
            self.busy = False
            self.result.show_error(item[1])
            self._tray_title("英译中助手 · 出错，见日志")
        elif kind == "quit":
            self.quit()

    # ---------------- 翻译流程 ----------------
    def _mode_of(self, text: str) -> str:
        """当前生效的模式：手动指定优先，自动模式按内容判断。"""
        if self.mode in ("translate", "zh2en", "summary", "term"):
            return self.mode
        if is_single_word(text):
            return "word"
        if has_cjk(text):
            return "zh2en"
        return "translate"

    def translate_pending(self) -> None:
        text, x, y = self.pending
        if not text:
            return
        if self.busy:
            return
        self.busy = True
        mode = self._mode_of(text)
        status = MODE_PENDING.get(mode, "正在翻译…")
        if not self.translator.ready:
            status = "正在加载本地模型（首次约 10 秒）…"
        self.result.open(x + 4, y + 12, text, status)
        threading.Thread(target=self._worker, args=(text, mode), daemon=True).start()

    def retry(self) -> None:
        if self.busy:
            return
        text, _x, _y = self.pending
        if text:
            self.busy = True
            mode = self._mode_of(text)
            self.result.clear()
            self.result.set_status("正在重新处理…")
            threading.Thread(target=self._worker, args=(text, mode), daemon=True).start()

    def _worker(self, text: str, mode: str = "translate") -> None:
        on_token = lambda piece: self.q.put(("token", piece))  # noqa: E731
        on_status = lambda status: self.q.put(("status", status))  # noqa: E731
        try:
            if mode == "word":
                result = self.translator.explain(text, on_token=on_token, on_status=on_status)
            elif mode == "term":
                result = self.translator.explain_term(text, on_token=on_token, on_status=on_status)
            elif mode == "summary":
                result = self.translator.summarize(text, on_token=on_token, on_status=on_status)
            elif mode == "zh2en":
                result = self.translator.translate(
                    text, on_token=on_token, on_status=on_status, direction="zh2en"
                )
            else:
                result = self.translator.translate(text, on_token=on_token, on_status=on_status)
            if result.text:
                self.q.put(("done", result, mode))
            else:
                self.q.put(("error", "没有拿到结果，请重试。"))
        except EngineError as exc:
            log.error("翻译失败: %s", exc)
            self.q.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            log.exception("翻译异常")
            self.q.put(("error", f"翻译失败：{exc}"))

    # ---------------- 托盘 ----------------
    def _tray_image(self):
        from PIL import Image, ImageDraw, ImageFont

        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((2, 2, size - 2, size - 2), radius=14, fill=(126, 190, 96, 255))
        font = None
        for path, index in ((r"C:\Windows\Fonts\msyhbd.ttc", 0), (r"C:\Windows\Fonts\msyh.ttc", 0)):
            if os.path.exists(path):
                try:
                    font = ImageFont.truetype(path, 40, index=index)
                    break
                except Exception:
                    font = None
        if font is not None:
            draw.text((size / 2, size / 2 - 2), "译", font=font, fill="white", anchor="mm")
        else:
            draw.text((size / 2, size / 2), "T", fill="white", anchor="mm")
        return img

    def _tray_title(self, text: str) -> None:
        try:
            if self.tray is not None:
                self.tray.title = text
        except Exception:
            pass

    def start_tray(self) -> None:
        import pystray

        mode_items = [
            pystray.MenuItem(
                MODE_LABELS[key],
                (lambda k=key: self.q.put(("set_mode", k))),
                checked=(lambda _item, k=key: self.mode == k),
                radio=True,
            )
            for key in MODE_CHOICES
        ]
        menu = pystray.Menu(
            pystray.MenuItem("显示翻译窗口", lambda: self.q.put(("show_window",)), default=True),
            pystray.MenuItem("模式", pystray.Menu(*mode_items)),
            pystray.MenuItem(
                "暂停划词", self._toggle_pause, checked=lambda _item: self.paused
            ),
            pystray.MenuItem(
                "开机自启", self._toggle_autostart,
                checked=lambda _item: bool(self.cfg.get("autostart")),
            ),
            pystray.MenuItem("重启本地引擎", self._restart_engine),
            pystray.MenuItem("打开日志目录", lambda: os.startfile(os.path.join(ROOT, "logs"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.q.put(("quit",))),
        )
        self.tray = pystray.Icon("eng2zh", self._tray_image(), "英译中助手 · 就绪", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()
        threading.Timer(1.5, self._startup_notify).start()
        log.info("托盘已启动")

    def _startup_notify(self) -> None:
        try:
            self.tray.notify(
                "已启动：选中英文后点「译」小图标；或选中后按 Ctrl+Alt+T 直接翻译。",
                "英译中助手",
            )
        except Exception:
            pass

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.watcher.enabled = not self.paused
        self.cfg["pause"] = self.paused
        config.save_config(self.cfg)
        log.info("划词监听: %s", "暂停" if self.paused else "开启")

    def _toggle_autostart(self) -> None:
        self.cfg["autostart"] = not bool(self.cfg.get("autostart"))
        try:
            set_autostart(self.cfg["autostart"])
            log.info("开机自启: %s", self.cfg["autostart"])
        except Exception as exc:
            log.error("设置开机自启失败: %s", exc)
        config.save_config(self.cfg)

    def _restart_engine(self) -> None:
        self._tray_title("英译中助手 · 重启引擎中")
        threading.Thread(target=self._do_restart, daemon=True).start()

    def _do_restart(self) -> None:
        try:
            self.translator.stop()
            self.translator.ensure_server()
            self._tray_title("英译中助手 · 就绪")
        except Exception as exc:
            log.error("重启引擎失败: %s", exc)
            self._tray_title("英译中助手 · 引擎重启失败")

    # ---------------- 生命周期 ----------------
    def warmup(self) -> None:
        """提前把模型载入显存，避免第一次翻译等待。"""
        def run() -> None:
            self._tray_title("英译中助手 · 加载模型…")
            self.translator.warmup()
            self._tray_title("英译中助手 · 就绪")
        threading.Thread(target=run, daemon=True).start()

    def run(self) -> None:
        self.watcher.start()
        self.root.after(50, self._drain)
        self.start_tray()
        self._show_window(first=True)  # 启动即显示一个空的翻译面板（固定右侧）
        self.dock.show()     # 任务栏上方常驻「译」按钮
        if self.mascot is not None and self._mascot_var.get():
            self.mascot.show()  # 右下角循环动图看板娘
        self._watch_click_through()
        self.warmup()
        log.info("程序已启动（build %s）", BUILD)
        self.root.mainloop()

    # ---------------- 面板自动穿透 ----------------
    def _our_hwnds(self) -> set:
        hwnds = set()
        wins = [self.result.win,
                self.result.bg.win if self.result.bg is not None else None,
                self.mode_popup.win,
                self.mascot.win if self.mascot is not None else None,
                self.dock.win]
        try:
            user32 = ctypes.windll.user32
            for w in wins:
                if w is not None:
                    h = user32.GetAncestor(w.winfo_id(), 2)
                    if h:
                        hwnds.add(h)
        except Exception:
            pass
        return hwnds

    def _watch_click_through(self) -> None:
        """面板闲置几秒后自动鼠标穿透：既能始终看见，又不会挡住划词。

        用户点「译」或选完文字出结果时会重置 6 秒窗口，这段时间内面板可正常操作。
        """
        try:
            ui = self.cfg["ui"]
            manual = bool(ui.get("panel_click_through", False))
            if self.result.visible():
                if manual:
                    self.result.apply_click_through(True)
                elif bool(ui.get("panel_auto_passthrough", True)):
                    try:
                        fg = ctypes.windll.user32.GetForegroundWindow()
                    except Exception:
                        fg = 0
                    if fg and fg in self._our_hwnds():
                        self.result.touch()
                    elif time.time() > getattr(self.result, "_interactive_until", 0.0):
                        self.result.apply_click_through(True)
                    else:
                        self.result.apply_click_through(False)
                else:
                    self.result.apply_click_through(False)
        except Exception:
            log.exception("自动穿透检测失败")
        if not self._shutting_down:
            self.root.after(250, self._watch_click_through)

    def quit(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("正在退出")
        # 1) 界面立刻消失，让用户马上看到反馈
        try:
            self.mode_popup.close()
        except Exception:
            pass
        for widget in (self.mascot, self.result, self.dock, self.icon):
            if widget is None:
                continue
            try:
                widget.hide()
            except Exception:
                pass
        try:
            if self.mascot is not None:
                self.mascot.destroy()
        except Exception:
            pass
        self._save_window_pos()
        # 2) 兜底看门狗：5 秒内没退干净就强制结束本进程
        try:
            def _force_exit() -> None:
                try:
                    if self.translator.proc is not None:
                        self.translator.proc.kill()
                except Exception:
                    pass
                os._exit(0)

            threading.Timer(5.0, _force_exit).start()
        except Exception:
            pass
        # 3) 正常清理
        for fn in (self.watcher.stop, self.translator.stop):
            try:
                fn()
            except Exception:
                pass
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        remove_pid_file()
        try:
            logging.shutdown()
        except Exception:
            pass
        os._exit(0)


# ---------------------------------------------------------------- CLI
def cmd_test(text: str, cfg: dict) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    translator = Translator(cfg)
    try:
        result = translator.translate(text)
        print(result.text)
        print(f"\n[{result.seconds:.2f}s, {result.tokens} tokens]", file=sys.stderr)
        return 0
    except EngineError as exc:
        print(f"引擎错误: {exc}", file=sys.stderr)
        return 1
    finally:
        translator.stop()


def cmd_warmup(cfg: dict) -> int:
    translator = Translator(cfg)
    try:
        translator.ensure_server()
        print("引擎已就绪", sys.stderr)
        return 0
    except EngineError as exc:
        print(f"引擎错误: {exc}", file=sys.stderr)
        return 1
    finally:
        translator.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="英译中划词翻译助手（纯本地，无 API）")
    parser.add_argument("--test", metavar="TEXT", help="命令行翻译一段文本后退出")
    parser.add_argument("--warmup", action="store_true", help="只启动引擎做预热后退出")
    parser.add_argument("--debug", action="store_true", help="输出调试日志到控制台")
    args = parser.parse_args(argv)

    cfg = config.load_config()
    setup_logging(debug=args.debug or bool(args.test))

    if args.test:
        return cmd_test(args.test, cfg)
    if args.warmup:
        return cmd_warmup(cfg)

    set_dpi_awareness()
    mutex = acquire_single_instance()
    if mutex is None:
        ctypes.windll.user32.MessageBoxW(
            None,
            "英译中助手已经在运行了。\n\n"
            "· 显示面板：点屏幕右下角（任务栏上方）的蓝色「译」按钮，或左键单击托盘图标\n"
            "· 退出程序：右键那个「译」按钮 → 退出程序\n"
            "· 如果卡住退不掉：双击目录里的 quit.bat 强制结束",
            "英译中助手",
            0x40,
        )
        return 0
    app = TranslatorApp(cfg)
    app._mutex = mutex
    write_pid_file()
    try:
        app.run()
    finally:
        try:
            app.translator.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
