"""Win32 RegisterHotKey 기반 전역 단축키.

키보드 후킹(keyboard/pynput)과 달리 모든 키 입력을 가로채지 않고,
등록한 조합만 OS가 알려주므로 관리자 권한이 필요 없고 보안 프로그램과 충돌이 적다.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable

from PyQt6.QtCore import QAbstractNativeEventFilter

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT, "win": MOD_WIN}


def parse_combo(combo: str) -> tuple[int, int] | None:
    mods, vk = 0, 0
    for part in (s.strip().lower() for s in str(combo).split("+")):
        if part in _MODS:
            mods |= _MODS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 12:
            vk = 0x70 + int(part[1:]) - 1
        else:
            return None
    return (mods, vk) if vk and mods else None


class HotkeyManager(QAbstractNativeEventFilter):
    def __init__(self, app, hwnd: int):
        super().__init__()
        self._user32 = ctypes.windll.user32
        self._hwnd = wintypes.HWND(hwnd)
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_id = 0xB000
        app.installNativeEventFilter(self)

    def register(self, combo: str, callback: Callable[[], None]) -> bool:
        parsed = parse_combo(combo)
        if parsed is None:
            return False
        mods, vk = parsed
        hotkey_id = self._next_id
        self._next_id += 1
        if not self._user32.RegisterHotKey(self._hwnd, hotkey_id, mods | MOD_NOREPEAT, vk):
            return False
        self._callbacks[hotkey_id] = callback
        return True

    def unregister_all(self) -> None:
        for hotkey_id in self._callbacks:
            self._user32.UnregisterHotKey(self._hwnd, hotkey_id)
        self._callbacks.clear()

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                callback = self._callbacks.get(int(msg.wParam))
                if callback is not None:
                    callback()
                    return True, 0
        return False, 0
