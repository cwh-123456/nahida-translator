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
from app.engine import (  # noqa: E402
    Cancel, Cancelled, EngineError, Translator, has_cjk, is_single_word, looks_glued,
)
from app.hotkey import HotkeyManager  # noqa: E402
from app.selection import SelectionWatcher  # noqa: E402
from app.shot import RegionSelector, grab_clipboard_image  # noqa: E402
from app.ui import DockButton, FloatingIcon, MascotWindow, ModePopup, ResultWindow  # noqa: E402
from app.vision import VisionTranslator  # noqa: E402

log = logging.getLogger("app")

MODE_CHOICES = ("auto", "translate", "zh2en", "summary", "term")
BUILD = "2026-10-28.1525"  # 启动日志里会打印，用于确认跑的是新代码

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
    "image": "图片识别（CPU）",
}

# 图片的提示词固定按"翻成中文"来，所以中译英模式下翻图也走翻译
IMAGE_MODE_ALIAS = {"auto": "translate", "zh2en": "translate"}

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
        self.vision = VisionTranslator(cfg)
        self.pending: tuple[str, int, int] = ("", 0, 0)
        self.busy = False
        # 面板当前显示的是不是图片翻译的结果。图片没有"原文"可改，
        # 点重译要把这种情形挡掉（否则会把"截图 800×600"当句子送去翻）。
        self._image_mode = False
        self._image_origin = ""
        # 任务代际：每开一轮翻译 +1。工作线程把 epoch 一起放进事件里，
        # 面板关掉后新开的那一轮不会被旧线程的结果覆盖。
        self._epoch = 0
        self._cancel: Cancel | None = None
        self.paused = bool(cfg.get("pause", False))
        self._shutting_down = False
        self._mutex = None
        self._mode_btn_rect = None      # 模式按钮的屏幕矩形，钩子线程里用纯算数判断
        self._hover_armed = True        # 悬停唤醒去抖：每次"进入可操作区"只投递一次
        self._shotting = False          # 框选截图进行中：钩子线程据此忽略划词
        self._hotkeys = HotkeyManager()
        self._shot_hotkey = ""          # 实际注册成功的截图热键（可能因占用而顺延）
        self._fullscreen_active = False  # 前台是否处于全屏（据此隐藏 dock/看板娘）

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
            on_dismiss=self._on_panel_dismiss,
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
            on_hotkey_image=self._clipboard_image_hotkey,
            on_single_click=self._on_panel_click,
            on_mouse_move=self._on_mouse_move,
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
            # 点「模式」按钮本身不算点外面：那是切换用的，
            # 否则按下先收起、松开又弹出，看起来就是「再点一次关不掉」
            if not self._mode_btn_hit(x, y):
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
        if self._shotting:
            # 框选界面自己处理 Esc；这里再投递一次会把翻译面板一起关掉
            return
        self.q.put(("escape",))

    def _on_panel_click(self, x: int, y: int) -> None:
        """单击落在面板上（钩子线程）：面板正在穿透的话，交给主线程判断要不要唤醒。

        为什么只在"单击"时做：拖选的按下也会经过钩子，如果按一下就唤醒，
        用户想选面板底下的文字时会被面板抢走整个拖拽。单击（几乎没位移）
        则是明确的"我要操作面板"——点叉号、点模式按钮、点复制。

        这里只做纯算数判断（钩子线程不能碰 Tk），落点到底在正文区还是标题带，
        交给主线程用控件几何去量。
        """
        if self.result.fast_visible() and self.result.click_through() \
                and self.result.fast_contains(x, y):
            self.q.put(("panel_click", x, y))

    def _apply_panel_click(self, x: int, y: int) -> None:
        """主线程：决定这一下点击要不要把面板从穿透状态唤醒。

        落在正文区就保持穿透——那是"我要选面板底下的字"，唤醒了反而挡住；
        落在标题带、按钮、边角上则是"我要操作面板"，唤醒并把层级摆正。
        """
        if not (self.result.visible() and self.result.click_through()):
            return
        if not self.result.fast_contains(x, y):
            return
        if self.result.click_in_body(x, y):
            return
        self.result.wake()

    def _on_mouse_move(self, x: int, y: int) -> None:
        """鼠标移动（钩子线程，极高频）：悬停到面板按钮区就提前解除穿透。

        这样"第一下点击"就打在面板上。之前只能靠单击后唤醒，是因为穿透状态
        下 Windows 已经把那一击派发给下面的程序，钩子救不回来——唯一可靠的办法
        是在鼠标"移过去"的那一刻就把穿透关掉。

        只做整数比较，且用 _hover_armed 去抖：每次进入只投递一次队列事件，
        否则每秒几百个事件会把主线程压垮。
        """
        try:
            if self._shotting:
                return
            if not self.result.fast_visible() or not self.result.click_through():
                # 面板没开 / 本来就可点：下次进入时重新允许投递
                self._hover_armed = True
                return
            if not self._hover_armed:
                return
            if self.result.action_zone_contains(x, y):
                self._hover_armed = False
                self.q.put(("panel_hover",))
        except Exception:
            pass

    def _apply_panel_hover(self) -> None:
        """主线程：悬停唤醒，只改穿透位，不动层级（层级交给 250ms 巡检）。"""
        if not self.result.visible():
            return
        self.result.hover_wake()

    def _panel_hit(self, x: int, y: int) -> bool:
        """面板是否在这个坐标上“真的挡住”鼠标（穿透状态下不算）。"""
        return self.result.fast_contains(x, y) and not self.result.click_through()

    def _should_ignore(self, x: int, y: int) -> bool:
        if self._shotting:
            # 框选期间整个屏幕都是我们的界面，别把拖拽当成划词
            return True
        return (
            self.icon.fast_contains(x, y)
            or self._panel_hit(x, y)
            or self.dock.fast_contains(x, y)
            or (self.mascot is not None and self.mascot.fast_contains(x, y))
        )

    # ---------------- 翻译面板 ----------------
    def _ready_status(self) -> str:
        if self._shot_hotkey:
            shot = f"截图按 {self._shot_hotkey_label()}"
        else:
            shot = "截图快捷键不可用，请用托盘菜单"
        if self.mode == "auto":
            return f"就绪 · 选中后按 Ctrl+Alt+T · {shot}"
        return (
            f"就绪 · {MODE_LABELS.get(self.mode, self.mode)} · "
            f"Ctrl+Alt+T 划词 · {shot}"
        )

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
            config.save_config(self.cfg, ["mode"])
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

    def _show_window(self, first: bool = False, focus_source: bool = False) -> None:
        """显示（或重新叫出）翻译面板；没有内容时显示空面板。

        first=True 表示程序刚启动：固定出现在屏幕右侧；
        其余情况（点「译」、托盘打开）沿用上次记住的位置。
        focus_source=True：用户是特意点开的，把光标直接放进原文框，
        这样不用先点一下输入框就能打字（启动时传 False，不抢焦点）。
        """
        if self.result.visible():
            # 面板已经开着：只把它和背景层抬到最前，不要重建内容
            self.result._raise_self()
            return
        pos = None if first else self.cfg.get("ui", {}).get("window_pos")
        self.result.show_empty(pos=tuple(pos) if pos else None)
        self._image_mode = False   # 空面板里没有图片原文，重译走普通文本
        self.result.set_mode(self.mode)
        self.result.set_status(self._ready_status())
        if focus_source:
            self.result.focus_source(force=True)

    def _save_window_pos(self) -> None:
        try:
            x, y = self.result.position
            ui = self.cfg.setdefault("ui", {})
            ui["window_pos"] = [int(x), int(y)]
            ui["popup_width"] = int(self.result.width)
            ui["popup_height"] = int(self.result.min_h)
            config.save_config(
                self.cfg, ["ui.window_pos", "ui.popup_width", "ui.popup_height"]
            )
        except Exception:
            pass

    # ---------------- 任务代际 / 取消 ----------------
    def _begin_job(self) -> tuple[int, Cancel]:
        """开一轮新任务，返回 (epoch, 取消信号)。

        上一轮要是还在跑（比如图片识别），先把它取消掉：两轮同时占着同一个
        llama-server，后来者只会排队等锁，还不如让旧的那轮立刻收手。
        """
        if self._cancel is not None and not self._cancel.cancelled():
            self._cancel.cancel("被新一轮翻译顶掉")
        self._epoch += 1
        self._cancel = Cancel()
        return self._epoch, self._cancel

    def _on_panel_dismiss(self) -> None:
        """用户关掉了面板（点叉 / Esc）= 不要这个结果了。

        图片识别在 CPU 上要跑十几秒，光把窗口藏起来不够：后台线程会继续跑到底，
        期间 busy 一直是 True，用户再截图/划词都被拦下，看起来像程序卡死。
        这里把取消信号发出去，工作线程在下一段流式输出时就会抛出 Cancelled 退出，
        连接随之关闭，引擎那边也会停。epoch 同时作废，残留事件一律丢弃。
        """
        self._epoch += 1
        if self._cancel is not None and not self._cancel.cancelled():
            self._cancel.cancel("面板已关闭")
            log.info("面板被关闭，取消进行中的任务（epoch=%d）", self._epoch)
        if self.busy:
            self.busy = False
            self._tray_title("英译中助手 · 已取消")

    @staticmethod
    def _mode_menu_anchor(btn) -> tuple[int, int]:
        if btn is not None:
            return btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height() + 6
        return 0, 0

    @staticmethod
    def _widget_rect(win) -> tuple[int, int, int, int] | None:
        """控件的屏幕矩形 (x, y, w, h)，拿到后就只是数字，钩子线程也能用。"""
        if win is None:
            return None
        try:
            return (
                int(win.winfo_rootx()), int(win.winfo_rooty()),
                int(win.winfo_width()), int(win.winfo_height()),
            )
        except Exception:
            return None

    def _mode_btn_hit(self, x: int, y: int) -> bool:
        rect = self._mode_btn_rect
        if not rect:
            return False
        bx, by, bw, bh = rect
        return bx <= x <= bx + bw and by <= y <= by + bh

    def _toggle_mode_popup(self, btn=None) -> None:
        """同一个模式浮层的两条入口共用：开着就收起，关着就在 btn 旁边打开。

        btn 为 None 表示从托盘菜单打开：没有可复用的按钮，点击别处照常收起。
        """
        if self.mode_popup.visible():
            self.mode_popup.close()
            return
        if btn is not None:
            x, y = self._mode_menu_anchor(btn)
            # 记下按钮矩形：钩子线程判断"点在按钮上"时不能碰 Tk，只能读这组数字
            self._mode_btn_rect = self._widget_rect(btn)
        else:
            px, py = self.root.winfo_pointerxy()
            x, y = px + 4, py + 6
            self._mode_btn_rect = None
        self.mode_popup.show(x, y, current=self.mode)

    def _show_panel_mode_menu(self) -> None:
        """翻译面板左上角「模式」按钮：点开自绘的模式浮层（再点一次收起）。"""
        self._toggle_mode_popup(self.result.mode_label)

    def _show_dock_mode_menu(self) -> None:
        self._toggle_mode_popup(None)

    def _toggle_mascot(self, show: bool | None = None) -> None:
        if self.mascot is None:
            return
        # 菜单项是 add_checkbutton + variable，点击时 Tk 会把变量翻成新状态再执行 command，
        # 所以这里要直接采用变量的当前值，不能再取反（取反会导致永远关不掉/开不起来）。
        enabled = bool(self._mascot_var.get()) if show is None else bool(show)
        self._mascot_var.set(enabled)
        self.cfg["ui"]["mascot_enabled"] = enabled
        try:
            config.save_config(self.cfg, ["ui.mascot_enabled"])
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
            config.save_config(self.cfg, ["ui.panel_click_through"])
        except Exception:
            pass
        self.result.apply_click_through(enabled)
        log.info("面板始终穿透: %s", enabled)

    def _toggle_auto_passthrough(self) -> None:
        enabled = bool(self._auto_pt_var.get())
        self.cfg["ui"]["panel_auto_passthrough"] = enabled
        try:
            config.save_config(self.cfg, ["ui.panel_auto_passthrough"])
        except Exception:
            pass
        log.info("面板闲置自动穿透: %s", enabled)

    def _show_dock_menu(self) -> None:
        """右下角常驻按钮的右键菜单（模式 / 显示面板 / 退出等）。"""
        if self._dock_menu is None:
            menu = tk.Menu(self.root, tearoff=0)
            menu.add_command(label="模式…", command=lambda: self.q.put(("dock_mode_popup",)))
            menu.add_command(label="显示翻译面板", command=lambda: self.q.put(("show_window",)))
            menu.add_separator()
            menu.add_command(
                label=f"截图翻译（{self._shot_hotkey_label()}）",
                command=lambda: self.q.put(("shot",)),
            )
            menu.add_command(
                label="翻译剪贴板图片", command=lambda: self.q.put(("clipboard_image",))
            )
            menu.add_separator()
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
        # token / status / done / error 都带着投递时的 epoch：面板被关掉或被新一轮
        # 顶掉之后，旧线程残余的事件必须丢掉，否则会把新面板的内容覆盖成旧结果
        if kind in ("token", "status", "done", "error", "source"):
            epoch = item[-1]
            if isinstance(epoch, int) and epoch != self._epoch:
                log.debug("丢弃过期任务事件 %s（epoch=%s，当前=%s）",
                          kind, epoch, self._epoch)
                return
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
        elif kind == "panel_click":
            self._apply_panel_click(item[1], item[2])
        elif kind == "panel_hover":
            self._apply_panel_hover()
        elif kind == "show_window":
            self._show_window(focus_source=True)
        elif kind == "dock_click":
            log.info("点击常驻按钮：面板可见=%s", self.result.visible())
            if self.result.visible():
                # 面板已经开着（里面通常有译文）：只抬到最前，不抢键盘焦点，
                # 免得用户正在别处打字时被拽过来
                self.result._raise_self()
            else:
                self._show_window(focus_source=True)
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
        elif kind == "shot":
            self._open_shot()
        elif kind == "image_ready":
            self.translate_image(item[1], item[2])
        elif kind == "clipboard_image":
            if not self._translate_clipboard_image():
                self.result.set_status("剪贴板里没有图片")
        elif kind == "escape":
            self.mode_popup.close()
            self.icon.hide()
            if self._card_has_focus():
                self.result.close()
        elif kind == "status":
            self.result.set_status(item[1])
        elif kind == "source":
            # 原文被"空格还原"改过了：把面板里的原文一起换掉，用户看到的就是修好的句子
            self.pending = (item[1], self.pending[1], self.pending[2])
            self.result.set_source(item[1])
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

    # ---------------- 截图 / 图片翻译 ----------------
    def _shot_hotkey_label(self) -> str:
        return (self._shot_hotkey or self.cfg.get("hotkey_shot", "ctrl+alt+d")).upper()

    def _request_shot(self) -> None:
        """热键线程进来：只投递事件，绝不能在这里碰 Tk。"""
        self.q.put(("shot",))

    def _open_shot(self) -> None:
        """全屏框选（在 Tk 主线程里跑一个局部事件循环，不阻塞后台线程）。"""
        if self._shotting:
            return
        if self.busy:
            log.info("上一次请求还没处理完，忽略截图热键")
            return
        self._shotting = True
        img = None
        try:
            self.icon.hide()
            self.mode_popup.close()
            img = RegionSelector(self.root).select()
        except Exception:
            log.exception("框选截图失败")
        finally:
            self._shotting = False
        if img is None:
            log.info("已取消框选（Esc / 右键 / 区域过小）")
            return
        self.translate_image(img, f"截图 {img.width}×{img.height}")

    def _translate_clipboard_image(self) -> bool:
        """剪贴板里是图片就翻图片（配合 Win+Shift+S）。返回 True 表示已接管。"""
        img = grab_clipboard_image()
        if img is None:
            return False
        self.translate_image(img, f"剪贴板图片 {img.width}×{img.height}")
        return True

    def _clipboard_image_hotkey(self) -> bool:
        """pynput 后台线程调用：不能碰 Tk，所以只读图 + 投递事件。

        判断要放在模拟 Ctrl+C 之前，否则那张图先被文本覆盖掉，就永远读不到了。
        """
        if self._shotting or self.busy:
            return False
        try:
            img = grab_clipboard_image()
        except Exception:
            log.exception("读取剪贴板图片失败")
            return False
        if img is None:
            return False
        self.q.put(("image_ready", img, f"剪贴板图片 {img.width}×{img.height}"))
        return True

    def translate_image(self, img, origin: str) -> None:
        """把一张 PIL 图送去视觉引擎翻译。"""
        if self.busy or img is None:
            return
        mode = IMAGE_MODE_ALIAS.get(self.mode, self.mode)
        if mode not in ("translate", "summary", "term"):
            mode = "translate"
        self.busy = True
        self.pending = ("", 0, 0)
        self._image_mode = True
        self._image_origin = origin
        if self.vision.available():
            status = "正在识别图片（纯 CPU，约 10~30 秒）…"
        else:
            status = "缺少视觉模型，见下方提示"
        # 图片翻译和划词翻译用同一套纳西妲绿配色，别一会儿白底一会儿绿底
        self.result.set_style("leaf")
        epoch, cancel = self._begin_job()
        self.result.open(0, 0, origin, status)
        log.info("开始图片翻译：%s，模式 %s（epoch=%d）", origin, mode, epoch)
        threading.Thread(
            target=self._worker_image, args=(img, mode, epoch, cancel), daemon=True
        ).start()

    def _worker_image(self, img, mode: str, epoch: int, cancel: Cancel) -> None:
        on_token = lambda piece: self.q.put(("token", piece, epoch))  # noqa: E731
        on_status = lambda status: self.q.put(("status", status, epoch))  # noqa: E731
        try:
            result = self.vision.translate_image(
                img, mode=mode, on_token=on_token, on_status=on_status, cancel=cancel
            )
            if result.text:
                self.q.put(("done", result, "image", epoch))
            else:
                self.q.put((
                    "error",
                    "没有从图片里读出内容。可能是图片太模糊、太小，或者框选的区域里没有文字。",
                    epoch,
                ))
        except Cancelled:
            # 用户自己关掉面板的：不算失败，什么都不用投递
            log.info("图片翻译已取消（epoch=%d）", epoch)
        except EngineError as exc:
            log.error("图片翻译失败: %s", exc)
            self.q.put(("error", str(exc), epoch))
        except Exception as exc:  # noqa: BLE001
            log.exception("图片翻译异常")
            self.q.put(("error", f"图片翻译失败：{exc}", epoch))

    def translate_pending(self) -> None:
        text, x, y = self.pending
        if not text:
            return
        if self.busy:
            return
        self.busy = True
        self._image_mode = False
        mode = self._mode_of(text)
        status = MODE_PENDING.get(mode, "正在翻译…")
        if not self.translator.ready:
            status = "正在加载本地模型（首次约 10 秒）…"
        self.result.set_style("leaf")
        epoch, cancel = self._begin_job()
        self.result.open(x + 4, y + 12, text, status)
        threading.Thread(
            target=self._worker, args=(text, mode, epoch, cancel), daemon=True
        ).start()

    def retry(self) -> None:
        if self.busy:
            return
        # 原文以输入框里的内容为准：用户可能改过划词得到的句子，
        # 也可能是点开空面板后自己敲进去的一段文字。
        text = self.result.get_source()
        if not text:
            # 空输入不猜、也不偷偷退回上一次的原文：直接提示，并把光标送进输入框
            self.result.set_status("请先在原文框里输入要翻译的内容")
            self.result.focus_source(force=True)
            return
        if self._image_mode and text == self._image_origin:
            # 图片翻译的"原文"只是一句说明，不是可译的句子
            self.result.set_status("图片翻译不能改原文重译，请重新截图框选")
            return
        self._image_mode = False
        self.pending = (text, self.pending[1], self.pending[2])
        self.busy = True
        mode = self._mode_of(text)
        status = MODE_PENDING.get(mode, "正在翻译…")
        if not self.translator.ready:
            status = "正在加载本地模型（首次约 10 秒）…"
        epoch, cancel = self._begin_job()
        self.result.clear()
        self.result.set_status(status)
        threading.Thread(
            target=self._worker, args=(text, mode, epoch, cancel), daemon=True
        ).start()

    def _restore_source_if_glued(self, text: str, epoch: int, on_status, cancel) -> str:
        """PDF 复制常见的"词粘成一团"：翻译前先把空格补回来，并同步到面板。

        只在检测到确实粘连时才做（见 engine.looks_glued），正常文本零开销。
        还原失败或模型动了内容时 engine 会原样返回，这里不需要兜底。
        """
        if not looks_glued(text):
            return text
        try:
            fixed = self.translator.restore_spaces(
                text, on_status=on_status, cancel=cancel)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 补空格失败不能拖累翻译
            log.warning("还原原文空格出错，按原文继续：%s", exc)
            return text
        if fixed and fixed != text:
            self.q.put(("source", fixed, epoch))
            return fixed
        return text

    def _worker(self, text: str, mode: str = "translate",
                epoch: int = 0, cancel: Cancel | None = None) -> None:
        on_token = lambda piece: self.q.put(("token", piece, epoch))  # noqa: E731
        on_status = lambda status: self.q.put(("status", status, epoch))  # noqa: E731
        try:
            text = self._restore_source_if_glued(text, epoch, on_status, cancel)
            if mode == "word":
                result = self.translator.explain(
                    text, on_token=on_token, on_status=on_status, cancel=cancel)
            elif mode == "term":
                result = self.translator.explain_term(
                    text, on_token=on_token, on_status=on_status, cancel=cancel)
            elif mode == "summary":
                result = self.translator.summarize(
                    text, on_token=on_token, on_status=on_status, cancel=cancel)
            elif mode == "zh2en":
                result = self.translator.translate(
                    text, on_token=on_token, on_status=on_status,
                    direction="zh2en", cancel=cancel
                )
            else:
                result = self.translator.translate(
                    text, on_token=on_token, on_status=on_status, cancel=cancel)
            if result.text:
                self.q.put(("done", result, mode, epoch))
            else:
                self.q.put(("error", "没有拿到结果，请重试。", epoch))
        except Cancelled:
            # 用户关掉面板 / 被新一轮顶掉：安静收工，不弹错误
            log.info("翻译已取消（epoch=%d）", epoch)
        except EngineError as exc:
            log.error("翻译失败: %s", exc)
            self.q.put(("error", str(exc), epoch))
        except Exception as exc:  # noqa: BLE001
            log.exception("翻译异常")
            self.q.put(("error", f"翻译失败：{exc}", epoch))

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
            pystray.MenuItem(
                f"截图翻译（{self._shot_hotkey_label()}）", lambda: self.q.put(("shot",))
            ),
            pystray.MenuItem(
                "翻译剪贴板图片", lambda: self.q.put(("clipboard_image",))
            ),
            pystray.Menu.SEPARATOR,
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
                f"已启动：选中文字按 Ctrl+Alt+T 翻译；"
                f"按 {self._shot_hotkey_label()} 框选截图翻译（支持公式和扫描件）。",
                "英译中助手",
            )
        except Exception:
            pass

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.watcher.enabled = not self.paused
        self.cfg["pause"] = self.paused
        config.save_config(self.cfg, ["pause"])
        log.info("划词监听: %s", "暂停" if self.paused else "开启")

    def _toggle_autostart(self) -> None:
        self.cfg["autostart"] = not bool(self.cfg.get("autostart"))
        try:
            set_autostart(self.cfg["autostart"])
            log.info("开机自启: %s", self.cfg["autostart"])
        except Exception as exc:
            log.error("设置开机自启失败: %s", exc)
        config.save_config(self.cfg, ["autostart"])

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
    def start_hotkeys(self) -> str:
        """注册截图热键。被别的程序占用时自动顺延到备用键，并把最终生效的键记下来。"""
        spec = str(self.cfg.get("hotkey_shot") or "ctrl+alt+d").strip().lower()
        fallbacks = [
            str(k).strip().lower()
            for k in (self.cfg.get("hotkey_shot_fallbacks") or [])
            if str(k).strip()
        ]
        self._hotkeys.add(spec, self._request_shot, fallbacks)
        try:
            chosen = self._hotkeys.start().get(spec)
        except Exception:
            log.exception("注册截图热键失败")
            chosen = None
        self._shot_hotkey = chosen or ""
        if chosen:
            log.info("截图热键已就绪: %s", chosen.upper())
        else:
            log.error("截图热键注册失败（%s 及其备用键都被占用），只能用托盘菜单", spec)
        return self._shot_hotkey

    def warmup(self) -> None:
        """提前把模型载入显存，避免第一次翻译等待。"""
        def run() -> None:
            self._tray_title("英译中助手 · 加载模型…")
            self.translator.warmup()
            self._tray_title("英译中助手 · 就绪")
        threading.Thread(target=run, daemon=True).start()

    def run(self) -> None:
        self.watcher.start()
        self.start_hotkeys()        # 先注册热键：托盘/右键菜单的标签要用最终生效的键
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

    def _foreground_fullscreen(self) -> bool:
        """前台窗口是否占满整个显示器（进入全屏的视频/游戏）。

        判定：取前台窗口的窗口矩形，和它所在显示器的矩形做比较；边框若
        铺满该显示器（含无边框全屏），即为全屏。排除桌面、任务栏和我们
        自己的窗口，避免误判。
        """
        try:
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            if not fg:
                return False
            if fg in self._our_hwnds():
                return False
            # 桌面本身（Progman / WorkerW）不算"进入全屏"
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(fg, cls, 64)
            if cls.value in ("Progman", "WorkerW"):
                return False
            # 前台窗口可见性：最小化/隐藏的窗口不判全屏
            if not user32.IsWindowVisible(fg):
                return False
            from ctypes import wintypes

            wr = wintypes.RECT()
            if not user32.GetWindowRect(fg, ctypes.byref(wr)):
                return False
            # 取该窗口所在显示器的工作区矩形（不是整屏，任务栏高度也算进窗口里）
            mon = user32.MonitorFromWindow(fg, 2)  # MONITOR_DEFAULTTONEAREST
            if not mon:
                return False

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                return False
            ww = wr.right - wr.left
            wh = wr.bottom - wr.top
            mw = mi.rcMonitor.right - mi.rcMonitor.left
            mh = mi.rcMonitor.bottom - mi.rcMonitor.top
            # 铺满整个显示器（宽高都达到），即全屏。留 2px 容差应对取整。
            return ww >= mw - 2 and wh >= mh - 2
        except Exception:
            return False

    def _watch_click_through(self) -> None:
        """面板闲置几秒后自动鼠标穿透：既能始终看见，又不会挡住划词。

        用户点「译」或选完文字出结果时会重置 6 秒窗口，这段时间内面板可正常操作。
        """
        try:
            # 全屏检测：前台进入全屏（视频/游戏）时，藏起常驻的「译」按钮和看板娘，
            # 退出全屏后按各自的开关状态恢复。看板娘只在用户开启时才重新显示。
            fs = self._foreground_fullscreen()
            if fs and not self._fullscreen_active:
                self._fullscreen_active = True
                self.dock.hide()
                if self.mascot is not None:
                    self.mascot.hide()
            elif not fs and self._fullscreen_active:
                self._fullscreen_active = False
                self.dock.show()
                if self.mascot is not None and self._mascot_var.get():
                    self.mascot.show()

            ui = self.cfg["ui"]
            manual = bool(ui.get("panel_click_through", False))
            # 兜底不变量：面板不可见时，背景层绝不能单独留在屏幕上
            # （背景层是独立窗口，任何未知路径把它重新显示出来，都会表现成"只剩一块动图"）
            if not self.result.visible():
                bg = getattr(self.result, "bg", None)
                if bg is not None and bg.win is not None and bg.win.winfo_ismapped():
                    bg.hide()
                    log.warning("面板不可见但背景层仍在显示，已强制隐藏")
            elif self.result.bg_order_broken():
                # 面板还在，但背景层翻到它上面去了。背景层是不透明的，
                # 表现出来跟"面板关了"一模一样，用户只会看到一块动图。
                self.result.keep_bg_below()
            # 另一个兜底不变量：背景层的真实矩形必须贴着面板。拖动时若某一次
            # SetWindowPos 没落到背景窗口上，Tk 的记录会和屏幕分叉，此后所有
            # sync() 都被"几何没变"挡掉，错位就永久留在那儿（"面板跑了、动图
            # 留在原地"）。这里用系统矩形核对，不一致就强制挪回去。
            if self.result.visible():
                self.result.keep_bg_aligned()
                self.result.release_stale_drag()
            # 浮层开着时刷新模式按钮矩形：面板被拖动后仍能正确判断"点在按钮上"
            if self.mode_popup.visible() and self.result.visible():
                self._mode_btn_rect = self._widget_rect(self.result.mode_label)
            if self.result.visible():
                # 正文区矩形的缓存，钩子线程靠它区分"正文"和"按钮区"
                self.result.refresh_zones()
                if manual:
                    self.result.apply_click_through(True)
                elif bool(ui.get("panel_auto_passthrough", True)):
                    try:
                        fg = ctypes.windll.user32.GetForegroundWindow()
                    except Exception:
                        fg = 0
                    # 鼠标正停在标题带/按钮上：别切回穿透，否则鼠标不动就会
                    # 一直"看得到点不着"（悬停唤醒只发生在移动的那一刻）
                    try:
                        hx, hy = self.root.winfo_pointerxy()
                        hovering = self.result.action_zone_contains(hx, hy)
                    except Exception:
                        hovering = False
                    if fg and fg in self._our_hwnds():
                        self.result.touch()
                    elif hovering:
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
                for proc_owner in (self.translator, self.vision):
                    try:
                        if proc_owner.proc is not None:
                            proc_owner.proc.kill()
                    except Exception:
                        pass
                os._exit(0)

            threading.Timer(5.0, _force_exit).start()
        except Exception:
            pass
        # 3) 正常清理
        for fn in (self.watcher.stop, self._hotkeys.stop,
                   self.vision.stop, self.translator.stop):
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


def cmd_test_image(path: str, cfg: dict) -> int:
    """命令行验证图片翻译：不启动界面，直接跑一次视觉引擎。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from PIL import Image

    vision = VisionTranslator(cfg)
    if not vision.available():
        print(vision.hint(), file=sys.stderr)
        return 2
    try:
        img = Image.open(path)
        img.load()
        print(f"图片: {path}  {img.width}×{img.height}", file=sys.stderr)
        result = vision.translate_image(img, on_status=lambda s: print(f"  {s}", file=sys.stderr))
        print(result.text)
        print(f"\n[{result.seconds:.1f}s, {result.tokens} tokens]", file=sys.stderr)
        return 0
    except EngineError as exc:
        print(f"引擎错误: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        vision.stop()


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
    parser.add_argument(
        "--test-image", metavar="PATH", help="命令行翻译一张图片后退出（验证视觉引擎）"
    )
    parser.add_argument("--warmup", action="store_true", help="只启动引擎做预热后退出")
    parser.add_argument("--debug", action="store_true", help="输出调试日志到控制台")
    args = parser.parse_args(argv)

    cfg = config.load_config()
    setup_logging(debug=args.debug or bool(args.test) or bool(args.test_image))

    if args.test:
        return cmd_test(args.test, cfg)
    if args.test_image:
        return cmd_test_image(args.test_image, cfg)
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
