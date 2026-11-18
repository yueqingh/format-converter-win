# -*- coding: utf-8 -*-
"""转换期间阻止电脑自动睡眠（仅 Windows 有效，其他系统什么也不做）。必须在界面主线程调用。"""
import os

import diag

log = diag.get_logger("电源")

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_on = False


def keep_awake(on):
    """on=True：告诉系统“还在工作，别睡眠”；False：恢复。返回是否成功（非 Windows 返回 False）。"""
    global _on
    if os.name != "nt":
        _on = False
        return False
    try:
        import ctypes
        flags = (_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED) if on else _ES_CONTINUOUS
        r = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        ok = bool(r)
        if ok != _on or on != _on:
            log.info("%s电脑自动睡眠：%s", "阻止" if on else "恢复", "成功" if ok else "失败")
        _on = bool(on and ok)
        return ok
    except Exception as e:
        log.warning("设置防睡眠失败：%s", e)
        return False


def is_on():
    return _on
