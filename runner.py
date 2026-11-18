# -*- coding: utf-8 -*-
"""转换执行器（不含界面）：探测、ffmpeg 进度、取消、语音识别、字幕导出、合并、试转。"""
import collections
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import wave

import core
import diag
import textfix

log = diag.get_logger("转换")


class RunError(Exception):
    """可直接展示给用户的错误。"""


class Cancelled(Exception):
    pass


class Item:
    _seq = 0

    def __init__(self, path):
        Item._seq += 1
        self.id = Item._seq
        self.path = path
        self.name = os.path.basename(path)
        self.probe = None
        self.probe_error = None
        self.settings = None          # None 表示使用全局设置
        self.status = "探测中"        # 探测中/等待/转换中/完成/失败/已取消/跳过
        self.pct = 0.0
        self.message = ""
        self.out_paths = []
        self.cmd_text = ""
        self.tail = collections.deque(maxlen=40)
        self.t0 = None
        self.elapsed = None
        self.trial_text = None
        self.notes = []
        self.snap = None              # 转换中途添加的文件：添加那一刻的界面设置
        self.in_batch = False         # 是否属于当前这一轮转换
        self.approx = False           # 百分比是否为估算
        self.eta = None               # 当前文件预计还需多少秒

    def brief(self):
        p = self.probe
        return "%s | %s | 状态=%s | 单独设置=%s | 命令=%s | 说明=%s | 错误输出末尾=%s" % (
            self.name, p.info() if p else ("探测失败: %s" % self.probe_error), self.status,
            "有" if self.settings else "无", self.cmd_text or "-", self.message or "-",
            " / ".join(list(self.tail)[-6:]) or "-")


# ---------------------------------------------------------------- 暂停/继续进程
def _nt_funcs():
    import ctypes
    k32, nt = ctypes.windll.kernel32, ctypes.windll.ntdll
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    nt.NtSuspendProcess.argtypes = [ctypes.c_void_p]
    nt.NtSuspendProcess.restype = ctypes.c_long
    nt.NtResumeProcess.argtypes = [ctypes.c_void_p]
    nt.NtResumeProcess.restype = ctypes.c_long
    return k32, nt


def _signal_proc(proc, suspend):
    """冻结/解冻一个子进程。成功返回 True。"""
    try:
        if os.name == "nt":
            k32, nt = _nt_funcs()
            h = k32.OpenProcess(0x0800, 0, proc.pid)   # PROCESS_SUSPEND_RESUME
            if not h:
                return False
            try:
                r = (nt.NtSuspendProcess if suspend else nt.NtResumeProcess)(h)
            finally:
                k32.CloseHandle(h)
            return r == 0
        os.kill(proc.pid, signal.SIGSTOP if suspend else signal.SIGCONT)
        return True
    except Exception as e:
        log.warning("%s进程失败：%s", "暂停" if suspend else "继续", e)
        return False


def suspend_proc(proc):
    return _signal_proc(proc, True)


def resume_proc(proc):
    return _signal_proc(proc, False)


class Ctx:
    """一次批量转换的运行环境。包含：取消、暂停/继续、先转某个文件（插队）、速度记忆。"""

    def __init__(self, ffmpeg, ffprobe, emit, tmp_dir, low_priority=True, out_mode="folder", out_custom="", sv=None,
                 data_dir=None, asr_opts=None):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.emit = emit
        self.tmp_dir = tmp_dir
        self.low_priority = low_priority
        self.out_mode = out_mode
        self.out_custom = out_custom
        self.sv = sv or {}
        self.data_dir = data_dir or os.path.dirname(tmp_dir)
        self.resume_dir = os.path.join(self.data_dir, "resume")
        self.speed_path = os.path.join(self.data_dir, "speed.json")
        self.asr_opts = {"simplified": True, "tidy": True, "split": True}
        self.asr_opts.update(asr_opts or {})
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.tl = threading.local()
        self.procs = {}              # 线路 -> 正在运行的子进程；线路：main（主队列）/ jump（先转的文件）
        self.suspended = set()
        self.paused = False
        self.pause_after = False
        self._pause_t0 = None
        self._paused_acc = 0.0
        self.jump_thread = None
        self.stats = {"ok": 0, "fail": 0, "cancel": 0, "outs": []}
        self.rates = {}              # 类型 -> 处理 1 秒内容要花的秒数
        self.reserved = set()

    @property
    def lane(self):
        return getattr(self.tl, "lane", "main")

    # ---- 子进程登记
    def set_proc(self, proc):
        with self.lock:
            lane = self.lane
            self.procs[lane] = proc
            if lane == "main" and self.paused and suspend_proc(proc):
                self.suspended.add("main")

    def clear_proc(self):
        with self.lock:
            lane = self.lane
            self.procs.pop(lane, None)
            self.suspended.discard(lane)

    def wake_for_kill(self, lane):
        """取消前先解冻，保证能被终止。"""
        with self.lock:
            p = self.procs.get(lane)
            if p is not None and lane in self.suspended:
                resume_proc(p)
                self.suspended.discard(lane)

    # ---- 暂停/继续
    def pause(self):
        with self.lock:
            if self.paused or self.cancel.is_set():
                return False
            self.paused = True
            self._pause_t0 = time.time()
            p = self.procs.get("main")
            if p is not None and p.poll() is None and suspend_proc(p):
                self.suspended.add("main")
        log.info("已暂停")
        self.emit("pause_state", None, True, "")
        return True

    def resume(self):
        with self.lock:
            if not self.paused:
                return False
            self.paused = False
            if self._pause_t0 is not None:
                self._paused_acc += time.time() - self._pause_t0
                self._pause_t0 = None
            p = self.procs.get("main")
            if p is not None and "main" in self.suspended:
                resume_proc(p)
            self.suspended.discard("main")
            self.cond.notify_all()
        log.info("已继续")
        self.emit("pause_state", None, False, "")
        return True

    def paused_acc(self):
        """当前线路累计被暂停的秒数（先转的文件不受暂停影响）。"""
        if self.lane != "main":
            return 0.0
        with self.lock:
            acc = self._paused_acc
            if self._pause_t0 is not None:
                acc += time.time() - self._pause_t0
            return acc

    def wait_if_paused(self):
        if self.lane != "main":
            return
        with self.lock:
            while self.paused and not self.cancel.is_set():
                self.cond.wait(0.3)

    def request_cancel(self):
        self.cancel.set()
        with self.lock:
            self.cond.notify_all()


class Clock:
    """只计算“真正在转换”的时间，暂停的时间不算。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.t0 = time.time()
        self.p0 = ctx.paused_acc()

    def elapsed(self):
        return max(0.0, time.time() - self.t0 - (self.ctx.paused_acc() - self.p0))


# ---------------------------------------------------------------- 探测
def probe_file(ffprobe, path):
    import json
    if not ffprobe:
        raise RunError("找不到 ffprobe（转换引擎不完整），请重新解压完整的程序包。")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise RunError("读取不了这个文件：%s" % e)
    t0 = time.time()
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
                           creationflags=core.popen_flags())
    except subprocess.TimeoutExpired:
        raise RunError("读取文件信息超时（超过 2 分钟），文件可能在慢速磁盘或网络位置。")
    except OSError as e:
        raise RunError("无法启动 ffprobe：%s" % e)
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = err[-1].split(": ", 1)[-1] if err else "未知原因"
        raise RunError("不是可识别的音视频文件，或文件已损坏。（%s）" % reason)
    try:
        data = json.loads(out)
    except Exception:
        raise RunError("读取文件信息失败（返回内容无法解析）。")
    p = core.parse_probe(data, path, size)
    if not p.v and not p.a:
        raise RunError("没有找到声音或画面，可能不是音视频文件，或文件已损坏。")
    log.info("探测完成 %s：%s（%.1fs）", os.path.basename(path), p.info(), time.time() - t0)
    return p


# ---------------------------------------------------------------- 进程执行
def _drain(stream, tail):
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                tail.append(line)
    except Exception:
        pass


def _watch_cancel(ctx, proc, lane="main"):
    while proc.poll() is None:
        if ctx.cancel.wait(0.2):
            try:
                ctx.wake_for_kill(lane)
                proc.terminate()
                try:
                    proc.wait(3)
                except Exception:
                    proc.kill()
            except Exception:
                pass
            return


def run_ffmpeg(ctx, argv, duration, on_progress, tail):
    """运行 ffmpeg，返回退出码；被取消抛 Cancelled。on_progress(百分比, 已用秒, 速度, 剩余秒)。"""
    ctx.wait_if_paused()
    if ctx.cancel.is_set():
        raise Cancelled()
    log.info("执行：%s", diag.redact(core.cmd_text(argv)))
    clock = Clock(ctx)
    lane = ctx.lane
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=core.popen_flags(ctx.low_priority))
    except OSError as e:
        raise RunError("无法启动转换引擎（ffmpeg）：%s" % e)
    ctx.set_proc(proc)
    threading.Thread(target=_drain, args=(proc.stderr, tail), daemon=True).start()
    threading.Thread(target=_watch_cancel, args=(ctx, proc, lane), daemon=True).start()
    cur = 0.0
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k in ("out_time_us", "out_time_ms"):
                try:
                    cur = max(0.0, int(v) / 1e6)
                except ValueError:
                    continue
            elif k == "progress":
                active = clock.elapsed()
                speed = cur / active if (active >= 1.5 and cur > 0) else 0.0
                pct = None
                eta = None
                if duration:
                    pct = min(99.5, cur / duration * 100.0)
                    if speed > 0:
                        eta = max(0.0, (duration - cur) / speed)
                on_progress(pct, active, speed, eta)
    finally:
        proc.wait()
        ctx.clear_proc()
    if ctx.cancel.is_set():
        raise Cancelled()
    log.info("ffmpeg 结束：退出码 %s，用时 %.1fs", proc.returncode, clock.elapsed())
    return proc.returncode


def _run_capture(ctx, argv, timeout):
    """运行一个短命令并收集它的错误输出（不解析进度）。返回 (退出码, 文字)。出错或超时返回 (None, "")。"""
    ctx.wait_if_paused()
    if ctx.cancel.is_set():
        raise Cancelled()
    lane = ctx.lane
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=core.popen_flags(ctx.low_priority))
    except OSError as e:
        log.warning("无法启动：%s", e)
        return None, ""
    ctx.set_proc(proc)
    chunks = []

    def rd():
        try:
            for raw in iter(proc.stderr.readline, b""):
                chunks.append(raw.decode("utf-8", "replace"))
        except Exception:
            pass
    th = threading.Thread(target=rd, daemon=True)
    th.start()
    threading.Thread(target=_watch_cancel, args=(ctx, proc, lane), daemon=True).start()
    clock = Clock(ctx)
    try:
        while proc.poll() is None:
            time.sleep(0.2)
            if clock.elapsed() > timeout:
                proc.kill()
                break
        proc.wait()
        th.join(3)
    finally:
        ctx.clear_proc()
    if ctx.cancel.is_set():
        raise Cancelled()
    return proc.returncode, "".join(chunks)


def friendly_ff_error(tail, rc):
    t = "\n".join(tail).lower()
    rules = [
        ("no space left", "磁盘空间不足，请清理空间或换一个保存位置。"),
        ("permission denied", "没有权限写入保存位置，请换一个文件夹。"),
        ("invalid data found", "文件损坏或格式不受支持。"),
        ("moov atom not found", "文件不完整（可能还没下载或录制完），无法读取。"),
        ("unknown encoder", "转换引擎缺少所需的编码器。"),
        ("could not find tag for codec", "这种声音/画面编码放不进所选的格式，请换一个目标格式。"),
        ("not currently supported in container", "这种编码放不进所选的格式，请换一个目标格式。"),
        ("too many packets buffered", "视频数据异常，请换用“重新编码”再试。"),
    ]
    for key, msg in rules:
        if key in t:
            return msg
    last = tail[-1] if tail else ""
    return "转换失败（错误码 %s）。%s" % (rc, ("详情：" + last) if last else "请点右上角“复制诊断报告”发给我。")


def _ok_output(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _rm(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _fmt_prog(pct, elapsed, speed, eta, approx=False):
    parts = []
    if pct is not None:
        parts.append(("约 %d%%" if approx else "%d%%") % int(pct))
    parts.append("已用 " + core.fmt_hms(elapsed))
    if speed:
        parts.append("速度 %.1f×" % speed)
    if eta is not None:
        parts.append("剩余约 " + core.fmt_hms(eta) + ("（估算）" if approx else ""))
    return " · ".join(parts)


def _kind_of(s):
    t = core.norm(s)["target"]
    return "asr" if t == "txt" else ("srt" if t == "srt" else "ff")


def _run_plan(ctx, item, plan, out_path, label="", pmap=None):
    """运行一个计划。pmap 用来把这一步的百分比换算成整个文件的百分比。"""
    item.cmd_text = plan.text()
    item.notes = list(plan.notes)
    item.tail.clear()
    kind = "ff"

    def prog(pct, el, sp, eta):
        item.approx = False
        item.eta = None if label else eta      # 语音转文字的“准备音频”阶段不参与整体剩余时间
        if sp and sp > 0 and not label:
            ctx.rates[kind] = 1.0 / sp
        total = pmap(pct) if (pmap and pct is not None) else pct
        ctx.emit("progress", item, total, label + _fmt_prog(pct, el, sp, eta))

    rc = run_ffmpeg(ctx, plan.argv, plan.duration, prog, item.tail)
    return rc


def check_space(path_dir, need_bytes):
    try:
        os.makedirs(path_dir, exist_ok=True)
        free = shutil.disk_usage(path_dir).free
        return free, free >= need_bytes
    except Exception:
        return None, True


# ---------------------------------------------------------------- 单个文件
def _decode_text(b):
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("gbk", "replace")


ASR_DEFAULT_RTF = 0.8        # 没有历史数据时，假设识别 1 秒声音要 0.8 秒
LAST_ASR_STATS = {}          # 最近一次识别的输出格式特征（不含内容），放进诊断报告
_speed_lock = threading.Lock()


def load_rtf(ctx):
    """读取这台电脑上记住的识别速度（处理 1 秒声音要花几秒）。"""
    try:
        with open(ctx.speed_path, "r", encoding="utf-8") as f:
            v = float(json.load(f).get("asr_rtf"))
        if 0.005 <= v <= 30:
            return v
    except Exception:
        pass
    return ASR_DEFAULT_RTF


def save_rtf(ctx, value):
    try:
        value = min(30.0, max(0.005, float(value)))
        with _speed_lock:
            old = None
            try:
                with open(ctx.speed_path, "r", encoding="utf-8") as f:
                    old = float(json.load(f).get("asr_rtf"))
            except Exception:
                pass
            new = value if old is None else 0.6 * value + 0.4 * old
            os.makedirs(os.path.dirname(ctx.speed_path), exist_ok=True)
            tmp = ctx.speed_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"asr_rtf": round(new, 4)}, f)
            os.replace(tmp, ctx.speed_path)
        ctx.rates["asr"] = new
    except Exception as e:
        log.warning("保存识别速度失败：%s", e)


def run_sensevoice(ctx, item, wav, on_tick=None, allow_empty=False):
    """识别一个 WAV，返回 (原始文字, 实际用时秒)。on_tick(已用秒) 约每 0.5 秒调用一次。"""
    ctx.wait_if_paused()
    if ctx.cancel.is_set():
        raise Cancelled()
    sv = ctx.sv
    exe, model, vad = sv.get("exe"), sv.get("model"), sv.get("vad")
    cmd = [exe, "-m", model]
    if vad:
        cmd += ["--vad", vad]
    cmd += ["-a", wav]
    if "SenseVoice" not in item.cmd_text:
        item.cmd_text += "\nSenseVoice：" + core.cmd_text(cmd)
    log.info("执行 SenseVoice：%s", diag.redact(core.cmd_text(cmd)))
    clock = Clock(ctx)
    lane = ctx.lane
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=os.path.dirname(exe) or None, creationflags=core.popen_flags(ctx.low_priority))
    except OSError as e:
        raise RunError("无法启动 SenseVoice 程序：%s\n请在“设置”里检查程序路径。" % e)
    ctx.set_proc(proc)
    err_tail = collections.deque(maxlen=40)
    threading.Thread(target=_drain, args=(proc.stderr, err_tail), daemon=True).start()
    threading.Thread(target=_watch_cancel, args=(ctx, proc, lane), daemon=True).start()
    box = []

    def rd():
        try:
            box.append(proc.stdout.read())
        except Exception:
            box.append(b"")
    th = threading.Thread(target=rd, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(0.5)
            if on_tick:
                try:
                    on_tick(clock.elapsed())
                except Exception:
                    pass
        proc.wait()
    finally:
        ctx.clear_proc()
    out = box[0] if box else b""
    if ctx.cancel.is_set():
        raise Cancelled()
    rc = proc.returncode
    active = clock.elapsed()
    log.info("SenseVoice 结束：退出码 %s，用时 %.1fs，输出 %d 字节", rc, active, len(out))
    item.tail.extend(err_tail)
    if rc != 0:
        code = rc & 0xFFFFFFFF
        if code == 0xC000001D:
            raise RunError("SenseVoice 程序与这台电脑的处理器不兼容（不支持它需要的指令）。\n请换用不带 AVX2 的版本：funasr-llamacpp-windows-x64.zip。")
        if code == 0xC0000135 or code == 0xC0000139:
            raise RunError("SenseVoice 程序缺少运行库，请安装“Microsoft Visual C++ 运行库”后重试。")
        last = err_tail[-1] if err_tail else ""
        raise RunError("语音识别失败（错误码 %s）。%s" % (rc, last))
    text = _decode_text(out).strip()
    if not text and not allow_empty:
        last = err_tail[-1] if err_tail else ""
        raise RunError("没有识别出文字（可能是静音、声音太小，或模型文件不对）。%s" % (("详情：" + last) if last else ""))
    return text, active


def _wav_info(path):
    """返回 (采样率, 声道数, 位宽, 总帧数)；读不了返回 None。"""
    try:
        with wave.open(path, "rb") as w:
            return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
    except Exception as e:
        log.warning("读取 WAV 头失败（将不分段）：%s", e)
        return None


def _write_chunk(src, dst, start, end):
    with wave.open(src, "rb") as w:
        rate, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        w.setpos(min(w.getnframes(), int(start * rate)))
        data = w.readframes(int((end - start) * rate))
    with wave.open(dst, "wb") as o:
        o.setnchannels(ch)
        o.setsampwidth(sw)
        o.setframerate(rate)
        o.writeframes(data)


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def clean_resume(data_dir, days=14):
    """删除超过 N 天没用的续转记录。"""
    try:
        root = os.path.join(data_dir, "resume")
        if not os.path.isdir(root):
            return
        now = time.time()
        for n in os.listdir(root):
            p = os.path.join(root, n)
            try:
                if os.path.isdir(p) and now - os.path.getmtime(p) > days * 86400:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except Exception:
        pass


def _resume_extra(ctx, s):
    a = core.asr_settings(s)
    sv = ctx.sv
    parts = [a["trim_start"], a["trim_end"], a["a_track"], a["a_channels"], a["a_volume"], a["a_gain"], "v1"]
    for k in ("model", "vad"):
        p = sv.get(k)
        try:
            parts.append("%s:%d" % (os.path.basename(p), os.path.getsize(p)) if p else "-")
        except OSError:
            parts.append("-")
    return "|".join(str(x) for x in parts)


def run_asr(ctx, item, wav, dur, s, trial, emit_stage):
    """识别一个已准备好的 WAV。返回 (原始文字, 续转记录文件夹或None)。
    emit_stage(进度0~1或None, 文字, 估算与否) 用来汇报识别阶段的进度。"""
    rtf0 = load_rtf(ctx)
    info = _wav_info(wav)
    total = (info[3] / float(info[0])) if info and info[0] else (dur or 0.0)
    chunks, rdir = [], None
    if (not trial and info and info[1] == 1 and info[2] == 2 and total >= core.CHUNK_MIN_TOTAL):
        key = core.resume_key(item.path, item.probe.size, os.path.getmtime(item.path), _resume_extra(ctx, s))
        rdir = os.path.join(ctx.resume_dir, key)
        os.makedirs(rdir, exist_ok=True)
        meta = os.path.join(rdir, "meta.json")
        try:
            with open(meta, "r", encoding="utf-8") as f:
                m = json.load(f)
            chunks = [(float(a), float(b)) for a, b in m["chunks"]]
            if abs(chunks[-1][1] - total) > 1.0:
                chunks = []
        except Exception:
            chunks = []
        if not chunks:
            emit_stage(0.0, "分析音频停顿位置，准备分段识别…", True)
            rc, txt = _run_capture(ctx, [ctx.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info", "-i", wav, "-af",
                                         "silencedetect=noise=-35dB:d=0.5", "-f", "null", "-"], max(120.0, total / 10))
            sil = core.parse_silencedetect(txt) if rc == 0 else []
            chunks = core.plan_chunks(total, sil)
            try:
                _atomic_write(meta, json.dumps({"chunks": chunks, "total": total}))
            except OSError as e:
                log.warning("写续转记录失败：%s", e)
            log.info("分段识别：总长 %.0f 秒，分 %d 段，停顿点 %d 个", total, len(chunks), len(sil))
    clock = Clock(ctx)

    if len(chunks) <= 1:
        est = (total or dur or 0.0) * rtf0

        def tick(act):
            if est > 0:
                frac = min(0.95, act / est)
                eta = max(0.0, est - act) if act < est else None
                msg = "识别中 · " + _fmt_prog(frac * 100, act, 0, eta, True) if act < est else \
                    "识别中 · 已用 %s · 比预计久，仍在识别…" % core.fmt_hms(act)
                item.eta = eta
                emit_stage(frac, msg, True)
            else:
                emit_stage(None, "识别中 · 已用 %s" % core.fmt_hms(act), False)
        tick(0.0)
        text, act = run_sensevoice(ctx, item, wav, tick)
        if total and act > 5:
            save_rtf(ctx, act / total)
        return text, None

    n = len(chunks)
    texts = [None] * n
    done_audio = 0.0
    run_audio = 0.0
    run_active = 0.0
    skipped = 0
    cwav = os.path.join(ctx.tmp_dir, "asr_%d_%d_chunk.wav" % (os.getpid(), item.id))
    try:
        for i, (a, b) in enumerate(chunks):
            if ctx.cancel.is_set():
                raise Cancelled()
            ctxt = os.path.join(rdir, "c%03d.txt" % i)
            if os.path.isfile(ctxt):
                try:
                    with open(ctxt, "r", encoding="utf-8") as f:
                        texts[i] = f.read()
                    done_audio += b - a
                    skipped += 1
                    continue
                except OSError:
                    pass
            _write_chunk(wav, cwav, a, b)
            clen = b - a

            def tick(act, i=i, clen=clen):
                rtf = (run_active / run_audio) if run_audio > 0 else rtf0
                rtf = max(rtf, 0.02)
                proc_audio = done_audio + min(clen * 0.95, act / rtf)
                frac = min(0.99, proc_audio / total) if total else None
                eta = max(0.0, (total - proc_audio) * rtf) if total else None
                item.eta = eta
                msg = "识别中 · 第 %d/%d 段 · %s" % (i + 1, n, _fmt_prog(frac * 100 if frac is not None else None,
                                                                    clock.elapsed(), 0, eta, True))
                emit_stage(frac, msg, True)
            tick(0.0)
            text, act = run_sensevoice(ctx, item, cwav, tick, allow_empty=True)
            _atomic_write(ctxt, text)
            texts[i] = text
            done_audio += clen
            run_audio += clen
            run_active += act
    finally:
        try:
            if os.path.exists(cwav):
                os.remove(cwav)
        except OSError:
            pass
    if skipped:
        item.notes.append("已从上次的进度接着识别：跳过了已完成的 %d 段（共 %d 段）。" % (skipped, n))
        log.info("续转：跳过 %d/%d 段", skipped, n)
    if run_audio >= 60:
        save_rtf(ctx, run_active / run_audio)
    raw = "\n".join(t for t in texts if t)
    if not raw.strip():
        raise RunError("没有识别出文字（可能是静音、声音太小，或模型文件不对）。")
    return raw, rdir


def _out_dir(ctx, item, trial):
    if trial:
        d = os.path.join(ctx.tmp_dir, "trial")
    else:
        d = core.out_dir_for(item.path, ctx.out_mode, ctx.out_custom)
    os.makedirs(d, exist_ok=True)
    return d


def _stem(item, trial):
    st = core.safe_name(os.path.splitext(item.name)[0])
    return ("试转-" + st) if trial else st


def process_item(ctx, item, s, limit=None, trial=False):
    s = core.norm(s)
    t = s["target"]
    p = item.probe
    if p is None:
        raise RunError("这个文件还没读取成功，不能转换。")
    if not os.path.isfile(item.path):
        raise RunError("找不到原文件（可能已被移动或删除）。")
    out_dir = _out_dir(ctx, item, trial)
    stem = _stem(item, trial)
    item.out_paths = []
    item.trial_text = None
    ff = ctx.ffmpeg
    if not ff:
        raise RunError("找不到转换引擎（ffmpeg），请重新解压完整的程序包。")

    if t == "srt":
        tracks = core.srt_tracks(p, s)
        n_all = s["sub_track"] == 0 and len(tracks) > 1
        for i, st in tracks:
            suffix = (".%d%s" % (i, ("." + st.lang) if st.lang else "")) if n_all else ""
            out = core.unique_path(os.path.join(out_dir, stem + suffix + ".srt"), ctx.reserved)
            plan = core.build_srt_cmd(ff, p, st, out)
            rc = _run_plan(ctx, item, plan, out)
            if rc != 0 or not _ok_output(out):
                _rm(out)
                raise RunError(friendly_ff_error(item.tail, rc))
            item.out_paths.append(out)
        return

    if t == "txt":
        sv = ctx.sv
        if not (sv.get("exe") and os.path.isfile(sv["exe"]) and sv.get("model") and os.path.isfile(sv["model"])):
            raise RunError("还没有设置 SenseVoice 的程序和模型路径。请点右上角“设置”选择。")
        if sv.get("vad") and not os.path.isfile(sv["vad"]):
            raise RunError("找不到 VAD 文件：%s\n请在“设置”里重新选择。" % sv["vad"])
        wav = os.path.join(ctx.tmp_dir, "asr_%d_%d.wav" % (os.getpid(), item.id))
        rdir = None
        try:
            plan = core.plan_asr_wav(ff, p, s, wav, limit)
            rc = _run_plan(ctx, item, plan, wav, "准备音频 · ", pmap=lambda x: x * 0.05)
            if rc != 0 or not _ok_output(wav):
                raise RunError(friendly_ff_error(item.tail, rc))

            def stage(frac, text, approx):
                item.approx = approx
                ctx.emit("progress", item, None if frac is None else 5.0 + 94.0 * frac, text)
            raw, rdir = run_asr(ctx, item, wav, plan.duration or p.duration or 0.0, s, trial, stage)
        finally:
            _rm(wav)
        o = ctx.asr_opts
        text = textfix.clean_text(raw, simplified=o.get("simplified", True), tidy=o.get("tidy", True),
                                  split=o.get("split", True))
        st = textfix.analyze_raw(raw)
        st["整理后字符数"] = len(text)
        LAST_ASR_STATS.clear()
        LAST_ASR_STATS.update(st)
        log.info("识别输出格式特征：%s", st)
        if not text.strip():
            raise RunError("识别结果整理后是空的。请取消“去掉识别标记并整理文字”再试，并把诊断报告发给我。")
        out = core.unique_path(os.path.join(out_dir, stem + ".txt"), ctx.reserved)
        with open(out, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(text + "\n")
        item.out_paths.append(out)
        item.trial_text = text
        if rdir:
            shutil.rmtree(rdir, ignore_errors=True)
        return

    # ---- 普通音视频
    out = core.unique_path(os.path.join(out_dir, stem + "." + core.OUT_EXT[t]), ctx.reserved)
    plan = core.plan_for(ff, p, s, out, limit)
    rc = _run_plan(ctx, item, plan, out)
    if (rc != 0 or not _ok_output(out)) and ("复制" in plan.mode or "原样" in plan.mode):
        log.warning("复制模式失败，自动改用重新编码重试：%s", item.name)
        _rm(out)
        s2 = dict(s)
        s2["force_encode"] = True
        plan = core.plan_for(ff, p, s2, out, limit)
        plan.notes.append("复制方式失败，已自动改用重新编码。")
        rc = _run_plan(ctx, item, plan, out)
    if rc != 0 or not _ok_output(out):
        _rm(out)
        raise RunError(friendly_ff_error(item.tail, rc))
    item.out_paths.append(out)


def process_merge(ctx, items, s):
    s = core.norm(s)
    probes = []
    for it in items:
        if it.probe is None:
            raise RunError("“%s”还没读取成功，不能合并。" % it.name)
        probes.append(it.probe)
    first = items[0]
    out_dir = _out_dir(ctx, first, False)
    stem = core.safe_name(os.path.splitext(first.name)[0]) + "-合并"
    out = core.unique_path(os.path.join(out_dir, stem + "." + core.OUT_EXT[s["target"]]), ctx.reserved)
    plan = core.plan_merge(ctx.ffmpeg, probes, s, out)
    first.cmd_text = plan.text()
    first.notes = list(plan.notes)
    first.tail.clear()

    def prog(pct, el, sp, eta):
        for it in items:
            ctx.emit("progress", it, pct, "合并中 · " + _fmt_prog(pct, el, sp, eta))

    rc = run_ffmpeg(ctx, plan.argv, plan.duration, prog, first.tail)
    if rc != 0 or not _ok_output(out):
        _rm(out)
        raise RunError(friendly_ff_error(first.tail, rc))
    for it in items:
        it.out_paths = [out]
    return out


# ---------------------------------------------------------------- 批量
def _pick_next(ctx, items):
    """取出下一个排队中的文件，并把它标记为“转换中”。线程安全。"""
    with ctx.lock:
        for it in list(items):
            if it.in_batch and it.status == "等待" and it.probe is not None:
                it.status, it.t0 = "转换中", time.time()
                return it
    return None


def _waiting_or_probing(ctx, items, include_waiting=True):
    with ctx.lock:
        for it in list(items):
            if it.in_batch and (it.status == "探测中" or (include_waiting and it.status == "等待" and it.probe is not None)):
                return True
    return False


def _run_one(ctx, it, settings_for, stats):
    """转换一个文件（状态已是“转换中”）。返回 ok / fail / cancel。"""
    ctx.emit("status", it, "转换中", "")
    clock = Clock(ctx)
    it.eta, it.approx, it.pct = None, False, 0.0
    try:
        s = settings_for(it)
        process_item(ctx, it, s)
        it.elapsed = clock.elapsed()
        it.status = "完成"
        ctx.emit("status", it, "完成", "")
        with ctx.lock:
            stats["ok"] += 1
            stats["outs"].extend(it.out_paths)
        try:
            dur = it.probe.duration
            kind = _kind_of(s)
            if dur and dur > 3 and kind != "srt":
                r = it.elapsed / dur
                old = ctx.rates.get(kind)
                ctx.rates[kind] = r if old is None else 0.5 * r + 0.5 * old
        except Exception:
            pass
        log.info("完成：%s → %s（用时 %s）", it.name, [os.path.basename(x) for x in it.out_paths], core.fmt_hms(it.elapsed))
        return "ok"
    except Cancelled:
        for o in it.out_paths:
            _rm(o)
        it.status = "已取消"
        ctx.emit("status", it, "已取消", "")
        with ctx.lock:
            stats["cancel"] += 1
        return "cancel"
    except (RunError, core.BuildError) as e:
        log.error("失败：%s：%s", it.name, e)
        it.status, it.message = "失败", str(e)
        ctx.emit("status", it, "失败", str(e))
        with ctx.lock:
            stats["fail"] += 1
        return "fail"
    except Exception as e:
        import sys
        diag.record_error("转换 " + it.name, *sys.exc_info())
        it.status = "失败"
        ctx.emit("status", it, "失败", "程序内部错误：%s，请复制诊断报告发给我。" % e)
        with ctx.lock:
            stats["fail"] += 1
        return "fail"
    finally:
        it.eta = None


def start_jump(ctx, it, settings_for):
    """暂停期间，先转所选文件；转完自动回到暂停的任务继续。成功返回 True。"""
    with ctx.lock:
        if not ctx.paused or ctx.jump_thread is not None or ctx.cancel.is_set():
            return False
        if not (it.in_batch and it.status == "等待" and it.probe is not None):
            return False
        it.status, it.t0 = "转换中", time.time()

        def work():
            ctx.tl.lane = "jump"
            try:
                _run_one(ctx, it, settings_for, ctx.stats)
            finally:
                with ctx.lock:
                    ctx.jump_thread = None
                ctx.emit("lane", None, "main", "")
                ctx.resume()
        t = threading.Thread(target=work, name="先转线程", daemon=True)
        ctx.jump_thread = t
    log.info("暂停期间先转：%s", it.name)
    ctx.emit("lane", it, "jump", "")
    t.start()
    return True


def estimate_remaining(ctx, items, settings_for):
    """估算整批还要多少秒；信息不够时返回 None。"""
    total = 0.0
    with ctx.lock:
        todo = [i for i in items if i.in_batch and i.status in ("转换中", "等待")]
    for it in todo:
        if it.status == "转换中":
            if it.eta is None:
                return None
            total += it.eta
            continue
        if it.probe is None or not it.probe.duration:
            return None
        kind = _kind_of(settings_for(it))
        if kind == "srt":
            continue
        rate = ctx.rates.get(kind)
        if rate is None and kind == "asr":
            rate = load_rtf(ctx)
        if rate is None:
            return None
        total += it.probe.duration * rate
    return total


def run_batch(ctx, items, settings_for, merge=False, merge_settings=None):
    """在后台线程里调用。items 是界面上的文件列表（转换过程中可能增减）。
    通过 ctx.emit 汇报：("status", item, 状态, 说明)、("progress", item, 百分比, 文字)、("batch_done", None, 统计)。"""
    stats = ctx.stats = {"ok": 0, "fail": 0, "cancel": 0, "outs": []}
    try:
        if merge:
            members = [i for i in items if i.in_batch]
            for it in members:
                it.status, it.t0 = "转换中", time.time()
                ctx.emit("status", it, "转换中", "")
            try:
                out = process_merge(ctx, members, merge_settings)
                for it in members:
                    it.elapsed = time.time() - it.t0
                    it.status = "完成"
                    ctx.emit("status", it, "完成", "")
                stats["ok"] += 1
                stats["outs"].append(out)
            except Cancelled:
                for it in members:
                    it.status = "已取消"
                    ctx.emit("status", it, "已取消", "")
                stats["cancel"] += 1
            except (RunError, core.BuildError) as e:
                for it in members:
                    it.status = "失败"
                    ctx.emit("status", it, "失败", str(e))
                stats["fail"] += 1
            except Exception as e:
                import sys
                diag.record_error("合并", *sys.exc_info())
                for it in members:
                    it.status = "失败"
                    ctx.emit("status", it, "失败", "程序内部错误：%s，请复制诊断报告发给我。" % e)
                stats["fail"] += 1
            return
        while not ctx.cancel.is_set():
            ctx.wait_if_paused()
            if ctx.cancel.is_set():
                break
            it = _pick_next(ctx, items)
            if it is None:
                if _waiting_or_probing(ctx, items):
                    time.sleep(0.3)
                    continue
                break
            if _run_one(ctx, it, settings_for, stats) == "cancel":
                break
            if ctx.pause_after:
                ctx.pause_after = False
                if _waiting_or_probing(ctx, items):
                    ctx.pause()
                    ctx.emit("pause_state", None, True, "after")
    finally:
        jt = ctx.jump_thread
        if jt is not None:
            jt.join(60)
        ctx.emit("batch_done", None, stats, "")


def run_trial(ctx, item, s, seconds=30):
    """试转前 N 秒，输出到临时文件夹。返回 (输出路径, 试转用时, 预计完整大小)。"""
    t0 = time.time()
    process_item(ctx, item, s, limit=seconds, trial=True)
    el = time.time() - t0
    out = item.out_paths[0] if item.out_paths else None
    est = None
    if out and os.path.isfile(out) and item.probe and item.probe.duration:
        ss = core.norm(s)
        try:
            st, dur = core.trim_range(item.probe, ss)
        except core.BuildError:
            dur = item.probe.duration
        full = dur or item.probe.duration
        got = min(seconds, full) if full else seconds
        if got > 0:
            est = os.path.getsize(out) / got * full
    return out, el, est
