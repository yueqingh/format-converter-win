# -*- coding: utf-8 -*-
"""自检：生成测试素材（文件名含中文和空格），跑完所有主要转换路径并核对结果。
既用于开发时测试，也被打包进程序（FormatConverter.exe --selftest）用于验证 Windows 环境。"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import core
import diag
import runner

FF = None
FP = None


def sh(args, **kw):
    r = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, creationflags=core.popen_flags(), **kw)
    if r.returncode != 0:
        raise RuntimeError("命令失败：%s\n%s" % (core.cmd_text(args), r.stderr.decode("utf-8", "replace")[-600:]))
    return r


def make_media(d):
    """返回 {名称: 路径}"""
    m = {}
    srt = os.path.join(d, "sub.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,500 --> 00:00:02,000\n你好，世界\n\n2\n00:00:02,500 --> 00:00:03,500\nHello\n")
    src = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4"]
    p = os.path.join(d, "测试 视频.mkv")
    sh([FF, "-y", *src, "-i", srt, "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-c:s", "srt", "-metadata:s:s:0", "language=chi", p])
    m["mkv"] = p
    p = os.path.join(d, "带字幕.mp4")
    sh([FF, "-y", *src, "-i", srt, "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-c:s", "mov_text", p])
    m["mp4sub"] = p
    p = os.path.join(d, "odd.mkv")
    sh([FF, "-y", "-f", "lavfi", "-i", "testsrc=size=321x241:rate=25:duration=2", "-f", "lavfi", "-i",
        "sine=frequency=500:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv444p", "-c:a", "aac", p])
    m["odd"] = p
    p = os.path.join(d, "无声.mp4")
    sh([FF, "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", p])
    m["noaudio"] = p
    p = os.path.join(d, "五点一 96k.wav")
    sh([FF, "-y", "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=96000:duration=3", "-ac", "6", p])
    m["wav51"] = p
    p = os.path.join(d, "歌曲.mp3")
    sh([FF, "-y", "-f", "lavfi", "-i", "sine=frequency=600:sample_rate=44100:duration=3", "-c:a", "libmp3lame", "-b:a", "128k", p])
    m["mp3"] = p
    p = os.path.join(d, "左右不同.wav")
    sh([FF, "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3", "-f", "lavfi", "-i",
        "sine=frequency=880:sample_rate=44100:duration=3", "-filter_complex", "[0][1]amerge=inputs=2", p])
    m["lr"] = p
    p = os.path.join(d, "长一点.mp4")
    sh([FF, "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=20", "-f", "lavfi", "-i",
        "sine=frequency=330:duration=20", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", p])
    m["long"] = p
    p = os.path.join(d, "不是媒体.ts")
    with open(p, "w", encoding="utf-8") as f:
        f.write("这只是一个文本文件\n" * 20)
    m["text"] = p
    return m


def make_fake_sv(d):
    if os.name == "nt":
        p = os.path.join(d, "fake_sv.bat")
        with open(p, "w", encoding="ascii") as f:
            f.write("@echo off\r\nset last=\r\n:loop\r\nif \"%~1\"==\"\" goto done\r\nset last=%~1\r\nshift\r\ngoto loop\r\n"
                    ":done\r\nif exist \"%last%\" (echo recognized-text-ok) else (exit /b 3)\r\n")
    else:
        p = os.path.join(d, "fake_sv.sh")
        with open(p, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nfor a; do last="$a"; done\n[ -f "$last" ] || exit 3\necho "识别结果测试 $(stat -c %s "$last")"\n')
        os.chmod(p, 0o755)
    return p


class Report:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail=""):
        self.rows.append((name, ok, detail))

    @property
    def failed(self):
        return [r for r in self.rows if not r[1]]

    def text(self):
        lines = ["自检结果：%d 项，失败 %d 项" % (len(self.rows), len(self.failed))]
        for n, ok, d in self.rows:
            lines.append("[%s] %s  %s" % ("通过" if ok else "失败", n, d))
        return "\n".join(lines)


def probe(path):
    return runner.probe_file(FP, path)


def run_selftest(workdir=None):
    global FF, FP
    rep = Report()
    FF, FP = core.find_tool("ffmpeg"), core.find_tool("ffprobe")
    rep.add("找到 ffmpeg / ffprobe", bool(FF and FP), "%s | %s" % (FF, FP))
    if not (FF and FP):
        return rep
    try:
        ver = subprocess.run([FF, "-version"], capture_output=True, creationflags=core.popen_flags()).stdout.decode("utf-8", "replace").splitlines()[0]
        rep.add("ffmpeg 版本", True, ver)
        diag.set_env_extra(ffmpeg=ver)
    except Exception as e:
        rep.add("ffmpeg 版本", False, str(e))
    own = workdir is None
    root = workdir or tempfile.mkdtemp(prefix="fc_selftest_")
    src = os.path.join(root, "素材 src")
    out = os.path.join(root, "输出 out")
    tmp = os.path.join(root, "tmp")
    for d in (src, out, tmp):
        os.makedirs(d, exist_ok=True)
    try:
        M = make_media(src)
        rep.add("生成测试素材（中文/空格文件名）", True, "%d 个" % len(M))
    except Exception as e:
        rep.add("生成测试素材", False, str(e))
        return rep

    events = []

    def mkctx(sv=None, low=True):
        return runner.Ctx(FF, FP, lambda *a: events.append(a), tmp, low_priority=low, out_mode="custom", out_custom=out, sv=sv)

    def convert(key, name, settings, check, limit=None, expect_note=None, expect_mode=None):
        try:
            it = runner.Item(M[key])
            it.probe = probe(M[key])
            ctx = mkctx()
            runner.process_item(ctx, it, settings, limit=limit)
            o = it.out_paths[0]
            pr = probe(o) if not o.endswith((".srt", ".txt")) else None
            ok, detail = check(o, pr, it)
            if expect_mode and expect_mode not in (it.cmd_text and _mode_of(it, settings, M[key])):
                ok, detail = False, "处理方式不符：期望含“%s”，实际“%s”" % (expect_mode, _mode_of(it, settings, M[key]))
            rep.add(name, ok, detail)
            return o
        except Exception as e:
            rep.add(name, False, "异常：%s" % (str(e) or e.__class__.__name__) + ("\n" + traceback.format_exc()[-400:] if not isinstance(e, (runner.RunError, core.BuildError)) else ""))
            return None

    def near(a, b, tol):
        return a is not None and abs(a - b) <= tol

    # ---------- 视频
    convert("mkv", "MKV→MP4（自动，应直接复制）", dict(target="mp4"),
            lambda o, p, it: (p.v.codec == "h264" and p.a[0].codec == "aac" and near(p.duration, 4, .4), p.info()),
            expect_mode="复制画面")
    convert("mkv", "MKV→MP4 缩到 120p", dict(target="mp4", v_res=120),
            lambda o, p, it: (p.v.h == 120 and p.v.w % 2 == 0, p.info()))
    convert("mkv", "MKV→MP4 15 帧", dict(target="mp4", v_fps=15, v_res=0, v_codec="h264"),
            lambda o, p, it: (abs(p.v.fps - 15) < 0.5, p.info()))
    convert("mkv", "MKV→MP4 保留字幕", dict(target="mp4", sub_mode="keep"),
            lambda o, p, it: (len(p.s) == 1 and p.s[0].codec == "mov_text", p.info()))
    convert("mp4sub", "MP4→MKV 保留字幕", dict(target="mkv", sub_mode="keep"),
            lambda o, p, it: (len(p.s) == 1, p.info()))
    convert("mkv", "MKV→WebM（VP9+Opus）", dict(target="webm"),
            lambda o, p, it: (p.v.codec == "vp9" and p.a[0].codec == "opus", p.info()))
    convert("mkv", "MKV→AVI（MPEG-4+MP3）", dict(target="avi"),
            lambda o, p, it: (p.v.codec == "mpeg4" and p.a[0].codec == "mp3", p.info()))
    convert("mkv", "MKV→MOV H.265", dict(target="mov", v_codec="h265"),
            lambda o, p, it: (p.v.codec == "hevc", p.info()))
    convert("mkv", "MKV→GIF", dict(target="gif", gif_width=160, gif_fps=10),
            lambda o, p, it: (p.v.codec == "gif" and p.v.w == 160 and not p.a, p.info()))
    convert("odd", "奇数尺寸 321×241 → MP4（自动补成偶数）", dict(target="mp4", v_codec="h264"),
            lambda o, p, it: (p.v.w % 2 == 0 and p.v.h % 2 == 0, p.info()))
    convert("mkv", "MKV→MP4 去掉声音", dict(target="mp4", a_mode="none"),
            lambda o, p, it: (not p.a and p.v is not None, p.info()))
    convert("mkv", "MKV→MP4 声音重新编码为 16kHz 单声道", dict(target="mp4", a_mode="encode", a_rate=16000, a_channels="mono"),
            lambda o, p, it: (p.a[0].rate == 16000 and p.a[0].ch == 1, p.info()))
    convert("noaudio", "无声视频→MP4", dict(target="mp4"),
            lambda o, p, it: (p.v is not None and not p.a, p.info()))
    convert("mkv", "剪切 1~3 秒（重新编码）", dict(target="mp4", trim_start="1", trim_end="3", v_codec="h264"),
            lambda o, p, it: (near(p.duration, 2, 0.35), p.info()))
    convert("mkv", "剪切 0:01~0:03（直接复制，允许误差）", dict(target="mp4", trim_start="0:01", trim_end="0:03"),
            lambda o, p, it: (p.duration is not None and 0.5 < p.duration < 4, p.info()))
    o = convert("long", "按目标大小压缩到 1MB（20 秒视频）", dict(target="mp4", size_mode="target", target_mb=1),
                lambda o, p, it: (os.path.getsize(o) < 1.25 * 1024 * 1024, "%s，实际 %s" % (p.info(), core.human_size(os.path.getsize(o)))))
    try:
        core.plan_for(FF, probe(M["mp3"]), dict(target="mp4"), "x.mp4")
        rep.add("纯音频转视频给出提示", False, "没有报错")
    except core.BuildError as e:
        rep.add("纯音频转视频给出提示", True, str(e)[:50])

    # ---------- 音频
    convert("mkv", "视频→MP3", dict(target="mp3"),
            lambda o, p, it: (p.v is None and p.a[0].codec == "mp3", p.info()))
    convert("mkv", "视频→WAV 16kHz 单声道 16 位（语音识别专用）", dict(target="wav", a_rate=16000, a_channels="mono", a_bits=16),
            lambda o, p, it: (p.a[0].rate == 16000 and p.a[0].ch == 1 and p.a[0].codec == "pcm_s16le", p.info()))
    convert("mkv", "视频→WAV 24 位", dict(target="wav", a_bits=24),
            lambda o, p, it: (p.a[0].codec == "pcm_s24le", p.info()))
    convert("mkv", "视频→FLAC 16 位", dict(target="flac", a_bits=16),
            lambda o, p, it: (p.a[0].codec == "flac", p.info()))
    convert("mkv", "视频→OGG", dict(target="ogg"),
            lambda o, p, it: (p.a[0].codec == "vorbis", p.info()))
    convert("mkv", "视频→Opus（44.1k 自动改 48k）", dict(target="opus"),
            lambda o, p, it: (p.a[0].codec == "opus", p.info()))
    convert("mkv", "视频→M4A（原样提取 AAC）", dict(target="m4a"),
            lambda o, p, it: (p.a[0].codec == "aac", p.info()), expect_mode="原样")
    convert("wav51", "5.1 声道 96kHz → MP3（自动降为立体声和 48k）", dict(target="mp3"),
            lambda o, p, it: (p.a[0].ch == 2 and p.a[0].rate <= 48000, p.info()))
    convert("mp3", "MP3→MP3（原样提取，不重新压缩）", dict(target="mp3"),
            lambda o, p, it: (p.a[0].codec == "mp3", p.info()), expect_mode="原样")
    convert("mp3", "MP3→MP3 改 96kbps（重新编码）", dict(target="mp3", a_bitrate=96),
            lambda o, p, it: (p.a[0].codec == "mp3" and 80000 < (p.a[0].bitrate or p.bitrate) < 112000, p.info()), expect_mode="重新编码")
    convert("mp3", "音量标准化后采样率保持 44100", dict(target="wav", a_volume="norm"),
            lambda o, p, it: (p.a[0].rate == 44100, p.info()))
    convert("mp3", "音量增益 +6dB", dict(target="wav", a_volume="gain", a_gain=6),
            lambda o, p, it: (p.a[0].codec == "pcm_s16le", p.info()))
    convert("mp3", "按目标大小：3 秒 MP3 压到 0.05MB", dict(target="mp3", size_mode="target", target_mb=0.05),
            lambda o, p, it: (p.a[0].codec == "mp3" and 30000 < os.path.getsize(o) < 62000,
                              "%s，实际 %s" % (p.info(), core.human_size(os.path.getsize(o)))))
    convert("lr", "只取左声道（频率应为 440Hz）", dict(target="wav", a_channels="left"),
            lambda o, p, it: (abs(_freq(o) - 440) < 15, "实测 %.0f Hz" % _freq(o)))
    convert("lr", "只取右声道（频率应为 880Hz）", dict(target="wav", a_channels="right"),
            lambda o, p, it: (abs(_freq(o) - 880) < 25, "实测 %.0f Hz" % _freq(o)))

    # ---------- 字幕
    convert("mkv", "提取内嵌字幕→SRT", dict(target="srt"),
            lambda o, p, it: ("你好，世界" in open(o, encoding="utf-8").read(), os.path.basename(o)))
    convert("mp4sub", "提取 mov_text 字幕→SRT", dict(target="srt", sub_track=0),
            lambda o, p, it: (os.path.getsize(o) > 10, os.path.basename(o)))

    # ---------- 语音识别（用假的 SenseVoice 程序验证流程）
    try:
        fake = make_fake_sv(tmp)
        dummy = os.path.join(tmp, "model.gguf")
        open(dummy, "wb").write(b"x")
        it = runner.Item(M["mkv"])
        it.probe = probe(M["mkv"])
        ctx = mkctx(sv={"exe": fake, "model": dummy, "vad": ""})
        runner.process_item(ctx, it, dict(target="txt"))
        txt = open(it.out_paths[0], encoding="utf-8-sig").read()
        rep.add("语音转文字流程（假程序）", "识别结果" in txt or "recognized" in txt, txt.strip())
        left = [f for f in os.listdir(tmp) if f.startswith("asr_")]
        rep.add("语音转文字后临时 WAV 已清理", not left, str(left))
        # 未设置路径
        try:
            runner.process_item(mkctx(sv={}), it, dict(target="txt"))
            rep.add("未设置 SenseVoice 路径时给出提示", False, "没有报错")
        except runner.RunError as e:
            rep.add("未设置 SenseVoice 路径时给出提示", "设置" in str(e), str(e))
        # 试转
        o, el, est = runner.run_trial(mkctx(), _prep(M["long"]), dict(target="mp4", v_codec="h264"), 5)
        rep.add("试转前 5 秒并估算完整大小", bool(o and os.path.isfile(o) and est and est > 0),
                "%s，估计 %s" % (core.human_size(os.path.getsize(o)), core.human_size(est or 0)))
    except Exception as e:
        rep.add("语音转文字/试转", False, "%s\n%s" % (e, traceback.format_exc()[-500:]))

    # ---------- 合并
    try:
        a, b = runner.Item(M["mkv"]), runner.Item(M["noaudio"])
        for x in (a, b):
            x.probe = probe(x.path)
        ctx = mkctx()
        o = runner.process_merge(ctx, [a, b], dict(target="mp4", v_codec="h264"))
        p = probe(o)
        rep.add("合并两个视频（其一无声，补静音；尺寸不同自动适配）", near(p.duration, 6, 0.6) and p.a != [], p.info())
        c, d = runner.Item(M["mp3"]), runner.Item(M["wav51"])
        for x in (c, d):
            x.probe = probe(x.path)
        o = runner.process_merge(mkctx(), [c, d], dict(target="mp3"))
        p = probe(o)
        rep.add("合并两个音频成 MP3", near(p.duration, 6, 0.5) and p.v is None, p.info())
        try:
            na = _prep(M["noaudio"])
            runner.process_merge(mkctx(), [c, na], dict(target="mp3"))
            rep.add("合并时有文件没声音给出提示", False, "没有报错")
        except (runner.RunError, core.BuildError) as e:
            rep.add("合并时有文件没声音给出提示", "没有声音" in str(e), str(e)[:60])
    except Exception as e:
        rep.add("合并", False, "%s\n%s" % (e, traceback.format_exc()[-500:]))

    # ---------- 取消
    try:
        it = runner.Item(M["long"])
        it.probe = probe(M["long"])
        ctx = mkctx()
        res = {}

        def work():
            try:
                runner.process_item(ctx, it, dict(target="mp4", v_codec="h265", v_speed="slow", v_res=0))
                res["r"] = "完成"
            except runner.Cancelled:
                res["r"] = "已取消"
            except Exception as e:
                res["r"] = "异常 %s" % e
        th = threading.Thread(target=work)
        th.start()
        time.sleep(1.5)
        t0 = time.time()
        ctx.cancel.set()
        th.join(15)
        rep.add("转换中取消", res.get("r") == "已取消" and time.time() - t0 < 6, "%s，%.1f 秒内停止" % (res.get("r"), time.time() - t0))
    except Exception as e:
        rep.add("取消", False, str(e))

    # ---------- 错误提示
    try:
        it = runner.Item(M["text"])
        try:
            runner.probe_file(FP, M["text"])
            rep.add("非媒体文件给出友好提示", False, "没有报错")
        except runner.RunError as e:
            rep.add("非媒体文件给出友好提示", True, str(e)[:60])
        pr = probe(M["noaudio"])
        try:
            core.plan_for(FF, pr, dict(target="mp3"), "x.mp3")
            rep.add("无声音文件转音频给出提示", False, "没有报错")
        except core.BuildError as e:
            rep.add("无声音文件转音频给出提示", True, str(e))
        try:
            core.parse_time("abc")
            rep.add("剪切时间格式错误提示", False, "没有报错")
        except ValueError as e:
            rep.add("剪切时间格式错误提示", True, str(e))
        try:
            core.plan_for(FF, probe(M["long"]), dict(target="mp4", size_mode="target", target_mb=0.001), "x.mp4")
            rep.add("目标大小过小提示", False, "没有报错")
        except core.BuildError as e:
            rep.add("目标大小过小提示", True, str(e))
    except Exception as e:
        rep.add("错误提示", False, str(e))

    # ---------- 诊断层
    try:
        t = diag.redact("联系 a.b@qq.com 手机 13812345678 token=abcdef C:\\Users\\张三\\Desktop 密钥 " + "A1" * 25)
        rep.add("脱敏", all(x not in t for x in ("a.b@qq.com", "13812345678", "abcdef", "张三", "A1A1A1")), t)
        r = diag.build_report()
        rep.add("诊断报告可生成", "诊断报告" in r and diag.APP_VERSION in r and "已脱敏字段" in r, "%d 字符" % len(r))
    except Exception as e:
        rep.add("诊断层", False, str(e))

    if own:
        try:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass
    return rep


def _mode_of(it, settings, path):
    """用同样的设置重新生成计划，取其处理方式文字。"""
    p = runner.probe_file(FP, path)
    s = core.norm(settings)
    k = core.target_kind(s["target"])
    plan = core.plan_for(FF, p, s, "x." + core.OUT_EXT[s["target"]])
    return plan.mode


def _freq(wav_path):
    """用过零点估算单声道 16 位 WAV 的频率。"""
    import wave, struct
    with wave.open(wav_path, "rb") as w:
        n, rate = w.getnframes(), w.getframerate()
        data = w.readframes(n)
    vals = struct.unpack("<%dh" % n, data) if w.getsampwidth() == 2 else []
    if not vals:
        return 0.0
    cross = sum(1 for i in range(1, len(vals)) if (vals[i - 1] < 0) != (vals[i] < 0))
    return cross / 2.0 / (n / float(rate))


def _prep(path):
    it = runner.Item(path)
    it.probe = probe(path)
    return it


if __name__ == "__main__":
    diag.init(os.path.dirname(os.path.abspath(__file__)))
    r = run_selftest()
    print(r.text())
    sys.exit(1 if r.failed else 0)
