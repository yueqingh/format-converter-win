# -*- coding: utf-8 -*-
"""转换执行器（不含界面）：探测、ffmpeg 进度、取消、语音识别、字幕导出、合并、试转。"""
import collections
import os
import shutil
import subprocess
import threading
import time

import core
import diag

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

    def brief(self):
        p = self.probe
        return "%s | %s | 状态=%s | 单独设置=%s | 命令=%s | 说明=%s | 错误输出末尾=%s" % (
            self.name, p.info() if p else ("探测失败: %s" % self.probe_error), self.status,
            "有" if self.settings else "无", self.cmd_text or "-", self.message or "-",
            " / ".join(list(self.tail)[-6:]) or "-")


class Ctx:
    """一次批量转换的运行环境。"""

    def __init__(self, ffmpeg, ffprobe, emit, tmp_dir, low_priority=True, out_mode="folder", out_custom="", sv=None):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.emit = emit
        self.tmp_dir = tmp_dir
        self.low_priority = low_priority
        self.out_mode = out_mode
        self.out_custom = out_custom
        self.sv = sv or {}
        self.cancel = threading.Event()
        self.proc = None
        self.reserved = set()


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


def _watch_cancel(ctx, proc):
    while proc.poll() is None:
        if ctx.cancel.wait(0.2):
            try:
                proc.terminate()
                try:
                    proc.wait(3)
                except Exception:
                    proc.kill()
            except Exception:
                pass
            return


def run_ffmpeg(ctx, argv, duration, on_progress, tail):
    """运行 ffmpeg，返回退出码；被取消抛 Cancelled。"""
    log.info("执行：%s", diag.redact(core.cmd_text(argv)))
    t0 = time.time()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=core.popen_flags(ctx.low_priority))
    except OSError as e:
        raise RunError("无法启动转换引擎（ffmpeg）：%s" % e)
    ctx.proc = proc
    threading.Thread(target=_drain, args=(proc.stderr, tail), daemon=True).start()
    threading.Thread(target=_watch_cancel, args=(ctx, proc), daemon=True).start()
    cur = 0.0
    speed = 0.0
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
            elif k == "speed":
                try:
                    speed = float(v.strip().rstrip("x"))
                except ValueError:
                    speed = 0.0
            elif k == "progress":
                pct = None
                eta = None
                if duration:
                    pct = min(99.5, cur / duration * 100.0)
                    if speed > 0:
                        eta = max(0.0, (duration - cur) / speed)
                on_progress(pct, time.time() - t0, speed, eta)
    finally:
        proc.wait()
    ctx.proc = None
    if ctx.cancel.is_set():
        raise Cancelled()
    log.info("ffmpeg 结束：退出码 %s，用时 %.1fs", proc.returncode, time.time() - t0)
    return proc.returncode


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


def _fmt_prog(pct, elapsed, speed, eta):
    parts = ["已用 " + core.fmt_hms(elapsed)]
    if speed:
        parts.append("速度 %.1f×" % speed)
    if eta is not None:
        parts.append("剩余约 " + core.fmt_hms(eta))
    return " · ".join(parts)


def _run_plan(ctx, item, plan, out_path, label=""):
    """运行一个计划；若是“复制”类且失败，自动改用重新编码再试一次由调用方处理。"""
    item.cmd_text = plan.text()
    item.notes = list(plan.notes)
    item.tail.clear()

    def prog(pct, el, sp, eta):
        ctx.emit("progress", item, pct, label + _fmt_prog(pct, el, sp, eta))

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


def run_sensevoice(ctx, item, wav):
    sv = ctx.sv
    exe, model, vad = sv.get("exe"), sv.get("model"), sv.get("vad")
    cmd = [exe, "-m", model]
    if vad:
        cmd += ["--vad", vad]
    cmd += ["-a", wav]
    item.cmd_text += "\n" + core.cmd_text(cmd)
    log.info("执行 SenseVoice：%s", diag.redact(core.cmd_text(cmd)))
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=os.path.dirname(exe) or None, creationflags=core.popen_flags(ctx.low_priority))
    except OSError as e:
        raise RunError("无法启动 SenseVoice 程序：%s\n请在“设置”里检查程序路径。" % e)
    ctx.proc = proc
    err_tail = collections.deque(maxlen=40)
    threading.Thread(target=_drain, args=(proc.stderr, err_tail), daemon=True).start()
    threading.Thread(target=_watch_cancel, args=(ctx, proc), daemon=True).start()
    out = proc.stdout.read()
    proc.wait()
    ctx.proc = None
    if ctx.cancel.is_set():
        raise Cancelled()
    rc = proc.returncode
    log.info("SenseVoice 结束：退出码 %s，用时 %.1fs，输出 %d 字节", rc, time.time() - t0, len(out))
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
    if not text:
        last = err_tail[-1] if err_tail else ""
        raise RunError("没有识别出文字（可能是静音、声音太小，或模型文件不对）。%s" % (("详情：" + last) if last else ""))
    return text


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
        try:
            plan = core.plan_asr_wav(ff, p, s, wav, limit)
            rc = _run_plan(ctx, item, plan, wav, "准备音频 · ")
            if rc != 0 or not _ok_output(wav):
                raise RunError(friendly_ff_error(item.tail, rc))
            ctx.emit("progress", item, None, "SenseVoice 识别中，请稍候…")
            text = run_sensevoice(ctx, item, wav)
        finally:
            _rm(wav)
        out = core.unique_path(os.path.join(out_dir, stem + ".txt"), ctx.reserved)
        with open(out, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(text + "\n")
        item.out_paths.append(out)
        item.trial_text = text
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
def run_batch(ctx, items, settings_for, merge=False, merge_settings=None):
    """在后台线程里调用。通过 ctx.emit 汇报：("status", item, 状态, 说明)、("progress", item, pct, 文字)、("batch_done", 统计)。"""
    stats = {"ok": 0, "fail": 0, "cancel": 0, "outs": []}
    try:
        if merge:
            for it in items:
                it.status, it.t0 = "转换中", time.time()
                ctx.emit("status", it, "转换中", "")
            try:
                out = process_merge(ctx, items, merge_settings)
                for it in items:
                    it.elapsed = time.time() - it.t0
                    ctx.emit("status", it, "完成", "")
                stats["ok"] += 1
                stats["outs"].append(out)
            except Cancelled:
                for it in items:
                    ctx.emit("status", it, "已取消", "")
                stats["cancel"] += 1
            except (RunError, core.BuildError) as e:
                for it in items:
                    ctx.emit("status", it, "失败", str(e))
                stats["fail"] += 1
            except Exception as e:
                import sys
                diag.record_error("合并", *sys.exc_info())
                for it in items:
                    ctx.emit("status", it, "失败", "程序内部错误：%s，请复制诊断报告发给我。" % e)
                stats["fail"] += 1
            return
        for it in items:
            if ctx.cancel.is_set():
                break
            if it.status not in ("等待", "失败", "已取消"):
                continue
            it.status, it.t0 = "转换中", time.time()
            ctx.emit("status", it, "转换中", "")
            try:
                process_item(ctx, it, settings_for(it))
                it.elapsed = time.time() - it.t0
                ctx.emit("status", it, "完成", "")
                stats["ok"] += 1
                stats["outs"].extend(it.out_paths)
                log.info("完成：%s → %s（用时 %s）", it.name, [os.path.basename(x) for x in it.out_paths],
                         core.fmt_hms(it.elapsed))
            except Cancelled:
                for o in it.out_paths:
                    _rm(o)
                ctx.emit("status", it, "已取消", "")
                stats["cancel"] += 1
                break
            except (RunError, core.BuildError) as e:
                log.error("失败：%s：%s", it.name, e)
                ctx.emit("status", it, "失败", str(e))
                stats["fail"] += 1
            except Exception as e:
                import sys
                diag.record_error("转换 " + it.name, *sys.exc_info())
                ctx.emit("status", it, "失败", "程序内部错误：%s，请复制诊断报告发给我。" % e)
                stats["fail"] += 1
    finally:
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
