# -*- coding: utf-8 -*-
"""诊断层：日志（内存环形缓冲 + 文件轮转）、全局错误捕获、操作轨迹、脱敏、诊断报告、可靠复制。
本模块的任何函数都不得抛出异常影响主程序。"""
import collections
import datetime
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import traceback

APP_NAME = "格式转换"
APP_VERSION = "1.1.0"
APP_DATE = "2026-11-18"

RING_SIZE = 200
TRAIL_SIZE = 60

_ring = collections.deque(maxlen=RING_SIZE)
_trail = collections.deque(maxlen=TRAIL_SIZE)
_errors = collections.deque(maxlen=10)
_providers = []          # [(标题, 函数)]，函数返回文本
_env_extra = {}
_state = {"data_dir": None, "log_file": None, "crash_cb": None, "inited": False}
REDACTED_FIELDS = "邮箱、手机号、password/token/key/secret/cookie 等字段的值、40 位以上长令牌串、Windows 用户名（路径中已替换为 <用户>）"

_lock = threading.Lock()


# ---------------------------------------------------------------- 脱敏
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_RE_KV = re.compile(r"(?i)\b(api[_-]?key|token|password|passwd|pwd|secret|cookie|authorization)\b(\s*[:=]\s*)\S+")
_RE_LONG = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{40,}(?![A-Za-z0-9+/=_-])")
_RE_USER = re.compile(r"(?i)([A-Z]:[\\/]Users[\\/])[^\\/\s\"']+")


def redact(text):
    try:
        t = str(text)
        t = _RE_KV.sub(lambda m: m.group(1) + m.group(2) + "<已脱敏>", t)
        t = _RE_EMAIL.sub("<邮箱>", t)
        t = _RE_PHONE.sub("<手机号>", t)
        t = _RE_USER.sub(lambda m: m.group(1) + "<用户>", t)

        def _long(m):
            s = m.group(0)
            if re.search(r"\d", s) and re.search(r"[A-Za-z]", s):
                return "<长令牌>"
            return s
        t = _RE_LONG.sub(_long, t)
        return t
    except Exception:
        return "<脱敏失败，内容已隐藏>"


# ---------------------------------------------------------------- 日志
class _RingHandler(logging.Handler):
    def emit(self, record):
        try:
            line = self.format(record)
            with _lock:
                _ring.append(line)
        except Exception:
            pass


def get_logger(name):
    return logging.getLogger("fc." + name)


def data_dir():
    return _state["data_dir"]


def init(base_dir):
    """base_dir：程序所在文件夹。优先在其下建 data 文件夹，不可写就改用用户目录。"""
    try:
        if _state["inited"]:
            return _state["data_dir"]
        d = None
        for cand in (os.path.join(base_dir, "data"),
                     os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "FormatConverter")):
            try:
                os.makedirs(cand, exist_ok=True)
                test = os.path.join(cand, ".w")
                with open(test, "w") as f:
                    f.write("1")
                os.remove(test)
                d = cand
                break
            except Exception:
                continue
        if d is None:
            import tempfile
            d = os.path.join(tempfile.gettempdir(), "FormatConverter")
            os.makedirs(d, exist_ok=True)
        _state["data_dir"] = d
        root = logging.getLogger("fc")
        root.setLevel(logging.INFO)
        root.propagate = False
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
        rh = _RingHandler()
        rh.setFormatter(fmt)
        root.addHandler(rh)
        try:
            lf = os.path.join(d, "app.log")
            fh = logging.handlers.RotatingFileHandler(lf, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            root.addHandler(fh)
            _state["log_file"] = lf
        except Exception:
            pass
        _state["inited"] = True
        install_hooks()
        get_logger("diag").info("启动 %s v%s，数据目录：%s", APP_NAME, APP_VERSION, redact(d))
        return d
    except Exception:
        return None


def trail(text):
    """记录一次用户操作。"""
    try:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with _lock:
            _trail.append("%s %s" % (ts, redact(text)))
        get_logger("操作").info(redact(text))
    except Exception:
        pass


def add_provider(title, fn):
    _providers.append((title, fn))


def set_env_extra(**kw):
    _env_extra.update(kw)


def set_crash_callback(cb):
    _state["crash_cb"] = cb


# ---------------------------------------------------------------- 全局错误捕获
def record_error(where, exc_type, exc, tb):
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with _lock:
            _errors.append("%s [%s]\n%s" % (ts, where, redact(text)))
        get_logger("异常").error("%s: %s", where, redact(repr(exc)))
        return text
    except Exception:
        return ""


def _auto_save_and_notify(where):
    try:
        rep = build_report()
        path = save_report(rep)
        cb = _state.get("crash_cb")
        if cb:
            cb(path)
    except Exception:
        pass


def install_hooks(tk_root=None):
    try:
        def _hook(exc_type, exc, tb):
            record_error("主线程未处理异常", exc_type, exc, tb)
            _auto_save_and_notify("main")
            try:
                sys.__excepthook__(exc_type, exc, tb)
            except Exception:
                pass
        sys.excepthook = _hook

        def _thook(args):
            record_error("后台线程异常(%s)" % getattr(args.thread, "name", "?"),
                         args.exc_type, args.exc_value, args.exc_traceback)
            _auto_save_and_notify("thread")
        threading.excepthook = _thook
        if tk_root is not None:
            def _tk(exc_type, exc, tb):
                record_error("界面回调异常", exc_type, exc, tb)
                _auto_save_and_notify("tk")
            tk_root.report_callback_exception = _tk
    except Exception:
        pass


def guard(where):
    """装饰器：捕获被装饰函数的异常并记录，不向外抛。"""
    def deco(fn):
        def inner(*a, **k):
            try:
                return fn(*a, **k)
            except Exception:
                et, ev, tb = sys.exc_info()
                record_error(where, et, ev, tb)
                return None
        inner.__name__ = getattr(fn, "__name__", "guarded")
        return inner
    return deco


# ---------------------------------------------------------------- 报告
def _env_text():
    lines = []
    try:
        lines.append("系统：%s %s (%s)" % (platform.system(), platform.release(), platform.version()))
        lines.append("架构：%s" % platform.machine())
        lines.append("Python：%s" % sys.version.split()[0])
        lines.append("是否打包运行：%s" % ("是" if getattr(sys, "frozen", False) else "否"))
        for k, v in _env_extra.items():
            lines.append("%s：%s" % (k, v))
        for mod in ("customtkinter", "tkinterdnd2"):
            try:
                m = __import__(mod)
                lines.append("%s：%s" % (mod, getattr(m, "__version__", "已加载")))
            except Exception as e:
                lines.append("%s：未加载（%s）" % (mod, e.__class__.__name__))
        try:
            import tkinter
            lines.append("Tk：%s" % tkinter.TkVersion)
        except Exception:
            pass
    except Exception as e:
        lines.append("（读取环境信息失败：%s）" % e)
    return "\n".join(lines)


def build_report(note=None):
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = []
        out.append("# %s 诊断报告" % APP_NAME)
        out.append("软件版本：v%s（%s）" % (APP_VERSION, APP_DATE))
        out.append("生成时间：%s" % now)
        out.append("已脱敏字段：%s" % REDACTED_FIELDS)
        if note:
            out.append("备注：%s" % redact(note))
        out.append("\n## 运行环境\n" + _env_text())
        for title, fn in list(_providers):
            try:
                out.append("\n## %s\n%s" % (title, redact(fn())))
            except Exception as e:
                out.append("\n## %s\n（读取失败：%s）" % (title, e))
        with _lock:
            tr = list(_trail)
            lg = list(_ring)
            er = list(_errors)
        out.append("\n## 最近用户操作（最多 %d 条）\n%s" % (TRAIL_SIZE, "\n".join(tr) or "（无）"))
        out.append("\n## 最近错误及堆栈\n%s" % ("\n\n".join(er) or "（无）"))
        out.append("\n## 最近日志（最多 %d 条）\n%s" % (RING_SIZE, "\n".join(lg) or "（无）"))
        return "\n".join(out)
    except Exception as e:
        return "# %s 诊断报告\n软件版本：v%s\n（生成报告时出错：%s）" % (APP_NAME, APP_VERSION, e)


def save_report(text):
    """保存为 diagnostic_report.txt，返回路径（失败返回 None）。"""
    for d in (_state.get("data_dir"), os.getcwd(), os.path.expanduser("~")):
        try:
            if not d:
                continue
            p = os.path.join(d, "diagnostic_report.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            return p
        except Exception:
            continue
    return None


def copy_text(tk_root, text):
    """复制到剪贴板：Tk 剪贴板（并回读校验）→ 失败返回 False，由界面弹出可全选的文本框 + 保存文件。"""
    try:
        tk_root.clipboard_clear()
        tk_root.clipboard_append(text)
        tk_root.update()
        try:
            back = tk_root.clipboard_get()
            if back and back.strip()[:50] == text.strip()[:50]:
                return True
        except Exception:
            pass
        # Windows 兜底：clip.exe
        if os.name == "nt":
            import subprocess
            p = subprocess.run(["clip"], input=text.encode("utf-16"), creationflags=0x08000000, timeout=10)
            if p.returncode == 0:
                return True
        return False
    except Exception:
        return False
