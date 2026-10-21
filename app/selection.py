"""划词捕获：鼠标拖选结束后模拟 Ctrl+C 取到选中文本，并还原剪贴板。

不读取目标程序的内部结构（无需 UIAutomation 权限），对浏览器、Word、PDF 阅读器、
记事本、VS Code 等绝大多数程序都通用。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time

import pyperclip
from pynput import keyboard, mouse

log = logging.getLogger("selection")

CF_UNICODETEXT = 13
_user32 = ctypes.windll.user32
_kb = keyboard.Controller()


def _clipboard_has_text() -> bool:
    try:
        return bool(_user32.IsClipboardFormatAvailable(CF_UNICODETEXT))
    except Exception:
        return True


def _read_clipboard() -> str:
    try:
        return pyperclip.paste() or ""
    except Exception:
        return ""


def _write_clipboard(text: str) -> None:
    try:
        pyperclip.copy(text)
    except Exception:
        pass


def send_copy(insert: bool = False) -> None:
    """向当前前台窗口发送 Ctrl+C（部分程序只认 Ctrl+Insert）。"""
    try:
        _kb.press(keyboard.Key.ctrl)
        time.sleep(0.01)
        if insert:
            _kb.press(keyboard.Key.insert)
            _kb.release(keyboard.Key.insert)
        else:
            _kb.press("c")
            _kb.release("c")
        time.sleep(0.01)
        _kb.release(keyboard.Key.ctrl)
    except Exception:
        log.exception("模拟复制按键失败")


# 抓取前先塞一个哨兵值：这样即使剪贴板里本来就是要选中的同一段文字，
# 也能检测到 Ctrl+C 是否真的生效（否则会误判为“没选中”）。
_PROBE = "\u200b__dsh_probe__\u200b"


def grab_selection(timeout: float = 0.6) -> str:
    """取当前选中文本；失败返回空串。结束后尽量还原原剪贴板文本。"""
    old = _read_clipboard()
    had_text = _clipboard_has_text()
    if had_text:
        _write_clipboard(_PROBE)
        time.sleep(0.02)
    # 等目标程序处理完鼠标松开再发复制键
    time.sleep(0.03)
    send_copy()
    deadline = time.time() + timeout
    new = _PROBE if had_text else old
    while time.time() < deadline:
        time.sleep(0.02)
        cur = _read_clipboard()
        if cur != new and cur != "":
            new = cur
            time.sleep(0.05)
            new = _read_clipboard() or new
            break
    if new == (_PROBE if had_text else old):
        # 再试一次 Ctrl+Insert
        send_copy(insert=True)
        deadline = time.time() + 0.35
        while time.time() < deadline:
            time.sleep(0.02)
            cur = _read_clipboard()
            if cur not in (new, "", _PROBE):
                new = cur
                time.sleep(0.05)
                new = _read_clipboard() or new
                break
    if had_text:
        _write_clipboard(old)
    text = (new or "").strip()
    # 有哨兵时，只要不是哨兵本身就说明复制成功了（即使内容和原剪贴板相同）
    if not text or text == _PROBE:
        log.info("剪贴板未变化（目标程序可能不支持复制，或没有选中内容）")
        return ""
    return text


class SelectionWatcher:
    """监听鼠标拖选与全局快捷键，把选中的文本回调给上层。"""

    def __init__(self, on_selection, should_ignore=None, on_press=None, hotkey="",
                 on_scroll=None, on_escape=None, on_hotkey_image=None):
        self.on_selection = on_selection
        self.should_ignore = should_ignore or (lambda x, y: False)
        self.on_press = on_press
        self.on_scroll = on_scroll
        self.on_escape = on_escape
        # 剪贴板里是图片时优先翻图（配合 Win+Shift+S）；返回 True 表示已接管
        self.on_hotkey_image = on_hotkey_image
        self.hotkey = hotkey
        self.enabled = True
        self._press_pos = None
        self._press_time = 0.0
        self._last_click = (0.0, 0, 0)
        self._last = ("", 0.0)
        self._busy = threading.Lock()
        self._mouse_listener = None
        self._key_listener = None
        self._esc_listener = None

    # ---------- 生命周期 ----------
    def start(self) -> None:
        self._start_listeners()
        self._watchdog_stop = False
        threading.Thread(target=self._watchdog, daemon=True).start()
        log.info("划词监听已启动（快捷键 %s）", self.hotkey or "关闭")

    def _start_listeners(self) -> None:
        try:
            self._mouse_listener = mouse.Listener(
                on_click=self._safe(self._on_click), on_scroll=self._safe(self._on_scroll)
            )
            self._mouse_listener.start()
        except Exception:
            log.exception("启动鼠标监听失败")
        if self.hotkey:
            try:
                self._key_listener = keyboard.GlobalHotKeys(
                    {self.hotkey: self._safe(self._hotkey_fired)}
                )
                self._key_listener.start()
            except Exception:
                log.exception("注册全局快捷键失败：%s", self.hotkey)
        if self.on_escape:
            try:
                self._esc_listener = keyboard.Listener(
                    on_press=self._safe(self._on_key_press)
                )
                self._esc_listener.start()
            except Exception:
                log.exception("注册 Esc 监听失败")

    @staticmethod
    def _safe(fn):
        """钩子回调里任何异常都不能抛出去（否则监听线程会直接挂掉）。"""
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                log.exception("钩子回调异常: %s", getattr(fn, "__name__", fn))
            return None
        return wrapper

    def _watchdog(self) -> None:
        """监听线程意外退出时自动重启（Windows 偶尔会静默移除低级钩子）。"""
        while not getattr(self, "_watchdog_stop", False):
            time.sleep(5)
            try:
                if self._mouse_listener is None or not self._mouse_listener.is_alive():
                    log.warning("鼠标监听线程已退出，自动重启")
                    self._start_listeners()
            except Exception:
                log.exception("看门狗重启监听失败")

    def stop(self) -> None:
        self._watchdog_stop = True
        for listener in (self._mouse_listener, self._key_listener, self._esc_listener):
            try:
                if listener is not None:
                    listener.stop()
            except Exception:
                pass

    # ---------- 鼠标 ----------
    def _on_scroll(self, x, y, dx, dy, *_extra) -> None:
        if self.on_scroll is None:
            return
        try:
            self.on_scroll(x, y, dx, dy)
        except Exception:
            pass

    def _on_key_press(self, key, *_extra) -> None:
        # pynput 1.8 也会多传一个 injected 参数
        if key == keyboard.Key.esc and self.on_escape:
            try:
                self.on_escape()
            except Exception:
                pass

    def _on_click(self, x, y, button, pressed, *_extra) -> None:
        # 注意：pynput 1.8 在 Windows 上会多传一个 injected 参数，用 *_extra 兼容
        if button != mouse.Button.left:
            return
        if pressed:
            self._press_pos = (x, y)
            self._press_time = time.time()
            if self.on_press:
                try:
                    self.on_press(x, y)
                except Exception:
                    pass
            return
        if self._press_pos is None:
            return
        px, py = self._press_pos
        self._press_pos = None
        if not self.enabled:
            return
        moved = ((x - px) ** 2 + (y - py) ** 2) ** 0.5
        duration = time.time() - self._press_time
        now = time.time()

        # 没怎么移动：可能是双击选词（双击/三击选中的文字没有拖动）
        if moved < 4:
            if self.should_ignore(px, py):
                return
            last_t, last_x, last_y = self._last_click
            near = ((x - last_x) ** 2 + (y - last_y) ** 2) ** 0.5 < 10
            if now - last_t <= 0.55 and near:
                self._last_click = (0.0, 0, 0)
                log.info("检测到双击选词，抓取选中文本")
                threading.Thread(target=self._capture, args=(x, y), daemon=True).start()
            else:
                self._last_click = (now, x, y)
            return

        if duration > 4.0:
            return
        # 关键：按「按下位置」判断，而不是松开位置。
        # 面板是半透明的，用户从别处开始拖选、松开时落到面板上属于正常操作。
        if self.should_ignore(px, py):
            log.info("拖选起点在工具窗口内，忽略")
            return
        log.info("检测到拖选: 位移 %.0fpx", moved)
        threading.Thread(target=self._capture, args=(x, y), daemon=True).start()

    def _capture(self, x: int, y: int) -> None:
        if not self._busy.acquire(blocking=False):
            return
        try:
            text = grab_selection()
            text = self._filter(text)
            if text:
                log.info("划词捕获 %d 字符: %s", len(text), text[:40].replace("\n", " "))
                self.on_selection(text, x, y, False)
            else:
                log.info("划词捕获为空：目标程序没有响应 Ctrl+C 或未选中内容")
        finally:
            self._busy.release()

    # ---------- 快捷键 ----------
    def _hotkey_fired(self) -> None:
        # 钩子线程里不能做耗时操作（会拖慢系统钩子），放到后台线程执行
        threading.Thread(target=self._hotkey_work, daemon=True).start()

    def _hotkey_work(self) -> None:
        if not self.enabled:
            return
        # 剪贴板里是图片时先翻图：模拟 Ctrl+C 会覆盖掉这张图，
        # 所以必须在抓文本之前判断（顺序错了就永远读不到图）。
        if self.on_hotkey_image is not None:
            try:
                if self.on_hotkey_image():
                    return
            except Exception:
                log.exception("剪贴板图片处理失败")
        if not self._busy.acquire(blocking=False):
            return
        try:
            text = self._filter(grab_selection())
            if text:
                pos = mouse.Controller().position
                # auto=True：快捷键触发时直接翻译，不再要求点图标
                self.on_selection(text, int(pos[0]), int(pos[1]), True)
        except Exception:
            log.exception("快捷键抓取失败")
        finally:
            self._busy.release()

    # ---------- 过滤 ----------
    def _filter(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) < 2:
            return ""
        if len(text) > 6000:
            text = text[:6000]
        # 允许字母、数字、中文；纯符号（如箭头、装饰线）才忽略
        if not any(ch.isalnum() for ch in text):
            log.info("忽略纯符号选区: %r", text[:20])
            return ""
        last_text, last_time = self._last
        if text == last_text and time.time() - last_time < 1.2:
            return ""
        self._last = (text, time.time())
        return text
