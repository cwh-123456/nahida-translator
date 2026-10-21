"""全局热键：用 Windows 的 RegisterHotKey 独占注册，而不是低级键盘钩子。

区别很关键：
  · 钩子是"广播"：QQ 截图和本程序会同时收到 Ctrl+Alt+S，必然双触发；
  · RegisterHotKey 是"占坑"：注册成功就独占，按下去只有本程序收到，
    别的程序再注册同一个组合会失败（错误码 1409）。

所以这里能明确知道某个键有没有被占用，并自动顺延到备用键，而不是按下没反应。
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

log = logging.getLogger("hotkey")

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN,
}


def parse_spec(spec: str) -> tuple[int, int]:
    """"ctrl+alt+d" → (修饰键掩码, 虚拟键码)。"""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("空的热键定义")
    key = parts[-1]
    mods = 0
    for p in parts[:-1]:
        if p not in _MODS:
            raise ValueError(f"不认识的修饰键: {p}")
        mods |= _MODS[p]
    if len(key) == 1:
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        named = {
            "space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B,
            "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
            "pgup": 0x21, "pgdn": 0x22, "printscreen": 0x2C,
        }
        if key not in named:
            raise ValueError(f"不认识的按键: {key}")
        vk = named[key]
    return mods, vk


def probe(spec: str) -> tuple[bool, int]:
    """只问一句"这个组合键现在能注册吗"，注册成功立刻注销。返回 (可用, 错误码)。"""
    try:
        mods, vk = parse_spec(spec)
    except ValueError:
        return False, -1
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    ctypes.set_last_error(0)
    ok = user32.RegisterHotKey(None, 0x4D53, mods | MOD_NOREPEAT, vk)
    err = ctypes.get_last_error()
    if ok:
        user32.UnregisterHotKey(None, 0x4D53)
        return True, 0
    return False, err


class HotkeyManager:
    """一个后台线程专门跑消息循环；回调只会被调用，不会阻塞消息泵（调用方应立刻投递队列）。"""

    def __init__(self) -> None:
        self._items: list[tuple[str, object]] = []
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._results: dict[str, str | None] = {}
        self._stop = False
        self._kept: list[int] = []

    def add(self, spec: str, callback, fallbacks: list[str] | None = None) -> None:
        """登记一个热键（必须在 start 之前调用）。fallbacks 依次作为被占用时的备选。"""
        cands = [spec] + [f for f in (fallbacks or []) if f]
        self._items.append((spec, callback, cands))

    def start(self, timeout: float = 6.0) -> dict[str, str | None]:
        """开始监听，返回 {原始键: 实际注册成功的键 或 None}。"""
        if not self._items:
            return {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return dict(self._results)

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            live: dict[int, object] = {}
            for idx, (spec, callback, cands) in enumerate(self._items, start=1):
                chosen = None
                for cand in cands:
                    try:
                        mods, vk = parse_spec(cand)
                    except ValueError as exc:
                        log.error("热键定义无效 %r: %s", cand, exc)
                        continue
                    ctypes.set_last_error(0)
                    if user32.RegisterHotKey(None, idx, mods | MOD_NOREPEAT, vk):
                        chosen = cand
                        live[idx] = callback
                        self._kept.append(idx)
                        break
                    err = ctypes.get_last_error()
                    if err == _ERROR_HOTKEY_ALREADY_REGISTERED:
                        log.warning("热键 %s 已被其他程序占用", cand)
                    else:
                        log.warning("注册热键 %s 失败（错误码 %s）", cand, err)
                self._results[spec] = chosen
                if chosen and chosen != spec:
                    log.warning("热键 %s 被占用，已自动改用 %s", spec, chosen)
                elif chosen:
                    log.info("热键已注册: %s", chosen)
            self._ready.set()

            msg = wintypes.MSG()
            while not self._stop:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    cb = live.get(int(msg.wParam))
                    if cb is not None:
                        try:
                            cb()
                        except Exception:
                            log.exception("热键回调异常")
        except Exception:
            log.exception("热键线程异常退出")
            self._ready.set()
        finally:
            for hid in self._kept:
                try:
                    user32.UnregisterHotKey(None, hid)
                except Exception:
                    pass
            self._kept.clear()

    def stop(self) -> None:
        self._stop = True
        try:
            if self._thread_id:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
