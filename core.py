# -*- coding: utf-8 -*-
"""核心逻辑（不含界面）：设置、预设、探测结果解析、ffmpeg 命令生成。"""
import json
import os
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------- 常量表
VIDEO_TARGETS = ["mp4", "mkv", "mov", "webm", "avi", "gif"]
AUDIO_TARGETS = ["mp3", "m4a", "wav", "flac", "ogg", "opus"]
SPECIAL_TARGETS = ["txt", "srt"]

TARGET_LABELS = [
    ("mp4", "视频 · MP4"), ("mkv", "视频 · MKV"), ("mov", "视频 · MOV"),
    ("webm", "视频 · WebM"), ("avi", "视频 · AVI"), ("gif", "视频 · GIF 动图"),
    ("mp3", "音频 · MP3"), ("m4a", "音频 · M4A (AAC)"), ("wav", "音频 · WAV"),
    ("flac", "音频 · FLAC 无损"), ("ogg", "音频 · OGG (Vorbis)"), ("opus", "音频 · Opus"),
    ("txt", "文字 · 语音识别 TXT"), ("srt", "字幕 · 提取内嵌 SRT"),
]
OUT_EXT = {"mp4": "mp4", "mkv": "mkv", "mov": "mov", "webm": "webm", "avi": "avi", "gif": "gif",
           "mp3": "mp3", "m4a": "m4a", "wav": "wav", "flac": "flac", "ogg": "ogg", "opus": "opus",
           "txt": "txt", "srt": "srt"}

QUALITY_LABELS = [("high", "高质量"), ("std", "标准"), ("small", "小文件")]
SPEED_LABELS = [("fast", "快（推荐旧电脑）"), ("balanced", "平衡"), ("slow", "慢（文件更小）")]
RES_LABELS = [(0, "原始"), (2160, "2160p (4K)"), (1080, "1080p"), (720, "720p"), (480, "480p")]
FPS_LABELS = [(0, "原始"), (60, "60"), (30, "30"), (25, "25"), (24, "24"), (15, "15")]
RATE_LABELS = [(0, "自动（保持原样）"), (8000, "8000 Hz"), (16000, "16000 Hz"), (22050, "22050 Hz"),
               (32000, "32000 Hz"), (44100, "44100 Hz"), (48000, "48000 Hz")]
CH_LABELS = [("auto", "自动（保持原样）"), ("mono", "单声道"), ("stereo", "立体声"),
             ("left", "只取左声道"), ("right", "只取右声道")]
BITS_LABELS = [(0, "自动"), (16, "16 位"), (24, "24 位"), (32, "32 位浮点")]
BR_LABELS = [(0, "自动"), (64, "64 kbps"), (96, "96 kbps"), (128, "128 kbps"), (160, "160 kbps"),
             (192, "192 kbps"), (256, "256 kbps"), (320, "320 kbps")]
VOL_LABELS = [("none", "不变"), ("norm", "自动标准化（音量均匀）"), ("gain", "手动增减")]
AMODE_LABELS = [("auto", "自动（推荐）"), ("copy", "复制（不重新编码）"), ("encode", "重新编码"), ("none", "去掉声音")]
SUB_LABELS = [("drop", "去掉字幕"), ("keep", "保留字幕")]
GIFW_LABELS = [(0, "原始宽度"), (320, "320"), (480, "480"), (640, "640")]
GIFFPS_LABELS = [(8, "8"), (10, "10"), (12, "12"), (15, "15"), (20, "20")]

V_ENC_LABELS = {"auto": "自动（推荐）", "copy": "复制（不重新编码）", "h264": "H.264（兼容最好）",
                "h265": "H.265（更小但更慢）", "vp9": "VP9", "mpeg4": "MPEG-4（老设备）"}
V_ENC_ALLOWED = {"mp4": ["h264", "h265"], "mov": ["h264", "h265"], "mkv": ["h264", "h265", "vp9"],
                 "webm": ["vp9"], "avi": ["mpeg4", "h264"]}
V_DEFAULT = {"mp4": "h264", "mov": "h264", "mkv": "h264", "webm": "vp9", "avi": "mpeg4"}
A_DEFAULT_ENC = {"mp4": "aac", "mov": "aac", "mkv": "aac", "webm": "libopus", "avi": "libmp3lame"}

V_COMPAT = {
    "mp4": {"h264", "hevc", "mpeg4", "av1"},
    "mov": {"h264", "hevc", "mpeg4", "prores", "mjpeg"},
    "mkv": {"h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "mpeg2video", "mpeg1video", "theora", "vc1", "mjpeg", "prores"},
    "webm": {"vp8", "vp9", "av1"},
    "avi": {"mpeg4", "h264", "mjpeg", "msmpeg4v3", "msmpeg4v2", "mpeg1video", "mpeg2video"},
}
A_COMPAT = {
    "mp4": {"aac", "mp3", "ac3", "eac3"},
    "mov": {"aac", "mp3", "ac3", "alac", "pcm_s16le", "pcm_s24le"},
    "mkv": {"aac", "mp3", "ac3", "eac3", "dts", "flac", "opus", "vorbis", "pcm_s16le", "pcm_s24le", "alac", "truehd"},
    "webm": {"opus", "vorbis"},
    "avi": {"mp3", "ac3", "pcm_s16le"},
}
A_COPY = {"mp3": {"mp3"}, "m4a": {"aac"}, "ogg": {"vorbis"}, "opus": {"opus"}, "flac": {"flac"}}

TEXT_SUB = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "microdvd", "subviewer",
            "subviewer1", "sami", "jacosub", "realtext", "stl", "vplayer", "mpl2", "pjs"}

CRF = {"h264": (18, 23, 28), "h265": (22, 28, 32), "vp9": (28, 33, 38)}
X264_PRESET = {"fast": "veryfast", "balanced": "medium", "slow": "slow"}
VP9_CPU = {"fast": "5", "balanced": "3", "slow": "1"}
MPEG4_Q = (2, 4, 7)
AUDIO_BR = {"aac": (256, 192, 128), "libmp3lame": (256, 192, 128), "libopus": (160, 128, 96)}
BR_RANGE = {"aac": (32, 512), "libmp3lame": (32, 320), "libopus": (8, 256), "libvorbis": (32, 500)}
RATE_ALLOWED = {
    "libmp3lame": [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000],
    "libopus": [8000, 12000, 16000, 24000, 48000],
}
DOWNMIX_STEREO = {"libmp3lame", "libvorbis", "libopus"}

DEFAULTS = dict(
    target="mp4", preset="", quality="std",
    a_rate=0, a_channels="auto", a_bits=0, a_bitrate=0, a_volume="none", a_gain=0.0, a_track=1, a_mode="auto",
    v_res=0, v_fps=0, v_codec="auto", v_speed="fast",
    sub_mode="drop", sub_track=1,
    gif_width=480, gif_fps=12,
    size_mode="quality", target_mb=50,
    trim_start="", trim_end="",
    force_encode=False,
)

BUILTIN_PRESETS = {
    "语音识别专用（16kHz 单声道 WAV）": dict(target="wav", a_rate=16000, a_channels="mono", a_bits=16),
    "通用音乐 MP3（192kbps）": dict(target="mp3", quality="std", a_bitrate=192),
    "手机/微信可播视频（MP4 720p）": dict(target="mp4", v_res=720, v_codec="h264", quality="std"),
    "无损保存（FLAC）": dict(target="flac"),
    "小文件视频（MP4 480p）": dict(target="mp4", v_res=480, quality="small"),
    "语音转文字（直接得到 TXT）": dict(target="txt"),
}
CUSTOM_PRESET = "（自定义）"


class BuildError(Exception):
    """可以直接展示给用户的中文错误。"""


def norm(s=None):
    d = dict(DEFAULTS)
    if s:
        for k, v in s.items():
            if k in d:
                d[k] = v
    for k in ("a_rate", "a_bits", "a_bitrate", "v_res", "v_fps", "gif_width", "gif_fps", "a_track", "sub_track"):
        try:
            d[k] = int(float(d[k]))
        except Exception:
            d[k] = DEFAULTS[k]
    try:
        d["a_gain"] = float(d["a_gain"])
    except Exception:
        d["a_gain"] = 0.0
    try:
        d["target_mb"] = max(0.05, float(d["target_mb"]))
    except Exception:
        d["target_mb"] = float(DEFAULTS["target_mb"])
    if d["target"] not in OUT_EXT:
        d["target"] = "mp4"
    return d


def preset_settings(name, user_presets=None):
    src = None
    if user_presets and name in user_presets:
        src = user_presets[name]
    elif name in BUILTIN_PRESETS:
        src = BUILTIN_PRESETS[name]
    if src is None:
        return None
    d = norm(src)
    d["preset"] = name
    return d


def target_kind(t):
    if t in VIDEO_TARGETS:
        return "video"
    if t in AUDIO_TARGETS:
        return "audio"
    return t  # txt / srt


def v_codec_options(target):
    if target not in V_ENC_ALLOWED:
        return []
    return [("auto", V_ENC_LABELS["auto"]), ("copy", V_ENC_LABELS["copy"])] + \
           [(k, V_ENC_LABELS[k]) for k in V_ENC_ALLOWED[target]]


# ---------------------------------------------------------------- 工具函数
def human_size(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.0f %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def fmt_hms(sec):
    if sec is None:
        return "未知"
    sec = max(0, int(round(sec)))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return "%02d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def ff_time(sec):
    return "%.3f" % sec


def parse_time(text):
    """'90' / '1:30' / '01:02:03.5' -> 秒；空白 -> None；格式错误 -> ValueError"""
    t = (text or "").strip().replace("：", ":")
    if not t:
        return None
    parts = t.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(\.\d+)?", x or "") for x in parts):
        raise ValueError("时间格式不对：%s（可写成 90、1:30 或 01:02:03）" % text)
    v = 0.0
    for x in parts:
        v = v * 60 + float(x)
    return v


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "output"


def unique_path(path, reserved=None):
    reserved = reserved if reserved is not None else set()
    base, ext = os.path.splitext(path)
    cand, i = path, 2
    while os.path.exists(cand) or cand.lower() in reserved:
        cand = "%s (%d)%s" % (base, i, ext)
        i += 1
    reserved.add(cand.lower())
    return cand


def out_dir_for(src_path, mode, custom=""):
    src_dir = os.path.dirname(os.path.abspath(src_path))
    if mode == "same":
        return src_dir
    if mode == "custom" and custom:
        return custom
    return os.path.join(src_dir, "转换结果")


def app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_tool(name, extra_dirs=()):
    exe = name + (".exe" if os.name == "nt" else "")
    base = app_base_dir()
    for d in list(extra_dirs) + [os.path.join(base, "ffmpeg"), base, os.path.join(base, "ffmpeg", "bin")]:
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


def popen_flags(low_priority=False):
    if os.name != "nt":
        return 0
    f = 0x08000000  # CREATE_NO_WINDOW
    if low_priority:
        f |= 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
    return f


def cmd_text(argv):
    return subprocess.list2cmdline([str(a) for a in argv])


# ---------------------------------------------------------------- 探测结果
class Stream:
    def __init__(self, **kw):
        self.index = 0
        self.codec = ""
        self.w = self.h = 0
        self.fps = 0.0
        self.rate = 0
        self.ch = 0
        self.bitrate = 0
        self.lang = ""
        self.title = ""
        self.pix_fmt = ""
        self.text = False
        self.__dict__.update(kw)

    def label(self, n):
        extra = " · ".join(x for x in (self.lang, self.title) if x)
        return "第%d条%s（%s）" % (n, (" · " + extra) if extra else "", self.codec)


class Probe:
    def __init__(self, path):
        self.path = path
        self.size = 0
        self.duration = None
        self.fmt = ""
        self.bitrate = 0
        self.v = None
        self.a = []
        self.s = []
        self.cover = False

    def kind(self):
        if self.v:
            return "视频"
        if self.a:
            return "音频"
        return "未知"

    def info(self):
        bits = [self.kind()]
        if self.v and self.v.w:
            bits.append("%d×%d" % (self.v.w, self.v.h))
        bits.append(fmt_hms(self.duration))
        if self.v:
            bits.append(self.v.codec.upper())
        if self.a:
            a = self.a[0]
            ch = {1: "单声道", 2: "立体声"}.get(a.ch, "%d声道" % a.ch if a.ch else "")
            bits.append("%s %s %s" % (a.codec.upper(), ("%dHz" % a.rate) if a.rate else "", ch))
            if len(self.a) > 1:
                bits.append("共%d条音轨" % len(self.a))
        if self.s:
            bits.append("%d条字幕" % len(self.s))
        bits.append(human_size(self.size))
        return " · ".join(b.strip() for b in bits if b and b.strip())

    def summary(self):
        return {"kind": self.kind(), "duration": self.duration, "size": self.size, "fmt": self.fmt,
                "v": self.v.__dict__ if self.v else None,
                "a": [x.__dict__ for x in self.a], "s": [x.__dict__ for x in self.s]}


def _fps(st):
    for key in ("avg_frame_rate", "r_frame_rate"):
        v = st.get(key) or ""
        try:
            n, d = v.split("/")
            n, d = float(n), float(d)
            if d and n:
                return n / d
        except Exception:
            continue
    return 0.0


def _rotation(st):
    try:
        r = (st.get("tags") or {}).get("rotate")
        if r is not None:
            return abs(int(float(r)))
        for sd in st.get("side_data_list") or []:
            if "rotation" in sd:
                return abs(int(float(sd["rotation"])))
    except Exception:
        pass
    return 0


def parse_probe(data, path, size=0):
    p = Probe(path)
    p.size = size
    fmt = data.get("format") or {}
    p.fmt = fmt.get("format_name", "")
    try:
        d = float(fmt.get("duration"))
        p.duration = d if d > 0 else None
    except Exception:
        p.duration = None
    try:
        p.bitrate = int(fmt.get("bit_rate") or 0)
    except Exception:
        p.bitrate = 0
    for st in data.get("streams") or []:
        ct = st.get("codec_type")
        tags = st.get("tags") or {}
        codec = st.get("codec_name") or "?"
        idx = int(st.get("index", 0))
        try:
            br = int(st.get("bit_rate") or 0)
        except Exception:
            br = 0
        if ct == "video":
            if (st.get("disposition") or {}).get("attached_pic"):
                p.cover = True
                continue
            if p.v is None:
                w, h = int(st.get("width") or 0), int(st.get("height") or 0)
                if _rotation(st) % 180 == 90:
                    w, h = h, w
                p.v = Stream(index=idx, codec=codec, w=w, h=h, fps=_fps(st), bitrate=br,
                             pix_fmt=st.get("pix_fmt") or "")
                if p.duration is None:
                    try:
                        p.duration = float(st.get("duration"))
                    except Exception:
                        pass
        elif ct == "audio":
            try:
                rate = int(st.get("sample_rate") or 0)
            except Exception:
                rate = 0
            p.a.append(Stream(index=idx, codec=codec, rate=rate, ch=int(st.get("channels") or 0), bitrate=br,
                              lang=tags.get("language", ""), title=tags.get("title", "")))
        elif ct == "subtitle":
            p.s.append(Stream(index=idx, codec=codec, lang=tags.get("language", ""), title=tags.get("title", ""),
                              text=codec in TEXT_SUB))
    if p.duration is None and p.a:
        try:
            p.duration = float(data["streams"][p.a[0].index].get("duration"))
        except Exception:
            pass
    return p


# ---------------------------------------------------------------- 命令生成
class Plan:
    def __init__(self, argv, duration=None, notes=None, mode=""):
        self.argv = argv
        self.duration = duration
        self.notes = notes or []
        self.mode = mode   # 例如“复制”“重新编码”，仅用于显示

    def text(self):
        return cmd_text(self.argv)


def base_cmd(ff, progress=True):
    c = [ff, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if progress:
        c += ["-progress", "pipe:1", "-stats_period", "0.5"]
    return c + ["-y"]


def _q(s):
    return {"high": 0, "std": 1, "small": 2}.get(s["quality"], 1)


def trim_range(p, s, limit=None):
    """返回 (start, dur)；dur 为 None 表示到结尾。"""
    st = parse_time(s["trim_start"]) or 0.0
    en = parse_time(s["trim_end"])
    if en is not None and en <= st:
        raise BuildError("剪切的结束时间必须大于开始时间。")
    if p.duration and st >= p.duration:
        raise BuildError("剪切的开始时间超过了文件总长度（%s）。" % fmt_hms(p.duration))
    dur = None
    if en is not None:
        dur = en - st
        if p.duration:
            dur = min(dur, p.duration - st)
    elif p.duration:
        dur = p.duration - st
    if limit is not None:
        dur = limit if dur is None else min(dur, limit)
    return st, dur


def audio_will_change(s):
    return bool(s["a_rate"] or s["a_channels"] != "auto" or s["a_bitrate"] or s["a_bits"]
                or s["a_volume"] != "none" or s["size_mode"] == "target")


def video_will_change(p, s):
    v = p.v
    if s["size_mode"] == "target":
        return True
    if s["v_res"] and v.h > s["v_res"]:
        return True
    if s["v_fps"] and v.fps and v.fps > s["v_fps"] + 0.5:
        return True
    return False


def audio_filters(s, src_ch):
    f = []
    ch = s["a_channels"]
    if ch in ("left", "right") and src_ch and src_ch >= 2:
        f.append("pan=mono|c0=c%d" % (0 if ch == "left" else 1))
    if s["a_volume"] == "norm":
        f.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    elif s["a_volume"] == "gain" and abs(s["a_gain"]) > 0.01:
        f.append("volume=%gdB" % s["a_gain"])
    return f


def channel_count(s, enc, src_ch):
    ch = s["a_channels"]
    if ch == "mono" or ch in ("left", "right"):
        return 1
    if ch == "stereo":
        return 2
    if enc in DOWNMIX_STEREO and src_ch and src_ch > 2:
        return 2
    return None


def _nearest(lst, v):
    return min(lst, key=lambda x: abs(x - v))


def pick_rate(s, enc, src_rate, notes):
    allowed = RATE_ALLOWED.get(enc)
    r = s["a_rate"] or None
    if r is None:
        if allowed and src_rate and src_rate not in allowed:
            r = _nearest(allowed, src_rate)
        elif s["a_volume"] == "norm":
            r = src_rate or 48000   # 标准化滤镜会改采样率，这里固定回原值
        return r
    if allowed and r not in allowed:
        n = _nearest(allowed, r)
        notes.append("该格式不支持 %d Hz，已改用 %d Hz。" % (r, n))
        r = n
    return r


def _lossy_kbps(enc, s, dur, notes, fixed_other=0):
    """按目标大小反推音频码率（仅音频输出时使用）。"""
    if not dur:
        raise BuildError("无法得知时长，不能按目标大小压缩，请改用“按质量”。")
    kbps = int(s["target_mb"] * 8192 * 0.97 / dur) - fixed_other
    lo, hi = BR_RANGE[enc]
    if kbps < lo:
        notes.append("目标大小太小，已用最低码率 %d kbps，结果会比目标大。" % lo)
    return max(lo, min(hi, kbps))


def audio_encode_args(enc, s, src_rate, src_ch, notes, kbps=None):
    q = _q(s)
    args = ["-c:a", enc]
    if enc == "libvorbis" and not s["a_bitrate"] and kbps is None:
        args += ["-q:a", str((7, 5, 3)[q])]
    else:
        br = kbps or s["a_bitrate"] or AUDIO_BR[enc][q]
        lo, hi = BR_RANGE[enc]
        args += ["-b:a", "%dk" % max(lo, min(hi, br))]
    rate = pick_rate(s, enc, src_rate, notes)
    if rate:
        args += ["-ar", str(rate)]
    ch = channel_count(s, enc, src_ch)
    if ch:
        args += ["-ac", str(ch)]
    flt = audio_filters(s, src_ch)
    if flt:
        args += ["-af", ",".join(flt)]
    return args


def pick_track(lst, n):
    if not lst:
        return None
    return lst[min(max(n, 1), len(lst)) - 1]


def sub_out_codec(target, codec):
    text = codec in TEXT_SUB
    if target in ("mp4", "mov"):
        return "mov_text" if text else None
    if target == "mkv":
        if codec in ("subrip", "srt", "ass", "ssa"):
            return "copy"
        return "srt" if text else "copy"
    if target == "webm":
        return "webvtt" if text else None
    return None


def venc_args(codec, s, container, kbps=None):
    q = _q(s)
    sp = s["v_speed"]
    a = []
    if codec == "h264":
        a += ["-c:v", "libx264", "-preset", X264_PRESET[sp]]
        a += (["-b:v", "%dk" % kbps, "-maxrate", "%dk" % int(kbps * 1.5), "-bufsize", "%dk" % (kbps * 2)]
              if kbps else ["-crf", str(CRF["h264"][q])])
        a += ["-pix_fmt", "yuv420p"]
    elif codec == "h265":
        a += ["-c:v", "libx265", "-preset", X264_PRESET[sp]]
        a += (["-b:v", "%dk" % kbps] if kbps else ["-crf", str(CRF["h265"][q])])
        a += ["-pix_fmt", "yuv420p", "-x265-params", "log-level=error"]
        if container in ("mp4", "mov"):
            a += ["-tag:v", "hvc1"]
    elif codec == "vp9":
        a += ["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", VP9_CPU[sp], "-row-mt", "1"]
        a += (["-b:v", "%dk" % kbps] if kbps else ["-crf", str(CRF["vp9"][q]), "-b:v", "0"])
        a += ["-pix_fmt", "yuv420p"]
    elif codec == "mpeg4":
        a += ["-c:v", "mpeg4", "-vtag", "xvid", "-pix_fmt", "yuv420p"]
        a += (["-b:v", "%dk" % kbps] if kbps else ["-q:v", str(MPEG4_Q[q])])
    else:
        raise BuildError("不支持的视频编码：%s" % codec)
    return a


def _target_video_kbps(s, dur, audio_kbps):
    if not dur:
        raise BuildError("无法得知时长，不能按目标大小压缩，请改用“按质量”。")
    total = s["target_mb"] * 8192 * 0.97 / dur
    v = int(total - audio_kbps)
    if v < 40:
        need = (40 + audio_kbps) * dur / 8192 / 0.97
        raise BuildError("目标大小 %g MB 对这段 %s 的视频太小，至少需要约 %.1f MB。" %
                         (s["target_mb"], fmt_hms(dur), need))
    return v


def plan_video(ff, p, s, out, limit=None):
    s = norm(s)
    t = s["target"]
    notes = []
    if not p.v:
        raise BuildError("这个文件没有画面，无法转成视频格式。只想要声音的话，请把“转换成”改成音频格式。")
    v = p.v
    start, dur = trim_range(p, s, limit)
    prog = dur if dur is not None else ((p.duration - start) if p.duration else None)
    cmd = base_cmd(ff)
    if start:
        cmd += ["-ss", ff_time(start)]
    cmd += ["-i", p.path]
    if dur is not None:
        cmd += ["-t", ff_time(dur)]

    if t == "gif":
        fps = s["gif_fps"] or 12
        chain = ["fps=%d" % fps]
        w = s["gif_width"]
        if w and v.w > w:
            chain.append("scale=%d:-2:flags=lanczos" % w)
        chain.append("split[s0][s1];[s0]palettegen=stats_mode=diff[pl];[s1][pl]paletteuse=dither=bayer:bayer_scale=5")
        graph = "[0:%d]%s" % (v.index, ",".join(chain))
        cmd += ["-filter_complex", graph, "-an", "-loop", "0", out]
        return Plan(cmd, prog, notes, "GIF 动图")

    # ---- 视频流
    vc = s["v_codec"]
    if s["force_encode"] and vc == "copy":
        vc = "auto"
    vc_ok = v.codec in V_COMPAT[t]
    copy_v = False
    if vc == "copy":
        if not vc_ok:
            raise BuildError("这个视频的编码（%s）不能直接放进 %s，请把“视频编码”改成“自动”或具体编码。" % (v.codec, t.upper()))
        copy_v = True
    elif vc == "auto":
        copy_v = vc_ok and not video_will_change(p, s) and not s["force_encode"]
        vc = V_DEFAULT[t]
    elif vc not in V_ENC_ALLOWED[t]:
        notes.append("%s 不适合 %s，已改用 %s。" % (V_ENC_LABELS.get(vc, vc), t.upper(), V_ENC_LABELS[V_DEFAULT[t]]))
        vc = V_DEFAULT[t]

    # ---- 音频流
    a = None if s["a_mode"] == "none" else pick_track(p.a, s["a_track"])
    if s["a_mode"] != "none" and not p.a:
        notes.append("原文件没有声音，输出也没有声音。")
    a_enc = A_DEFAULT_ENC[t]
    copy_a = False
    if a:
        a_ok = a.codec in A_COMPAT[t] and not s["force_encode"]
        if s["a_mode"] == "copy" and s["force_encode"]:
            pass
        elif s["a_mode"] == "copy":
            if not a_ok:
                raise BuildError("这个音频的编码（%s）不能直接放进 %s，请把“声音”改成“自动”或“重新编码”。" % (a.codec, t.upper()))
            copy_a = True
        elif s["a_mode"] == "auto":
            copy_a = a_ok and not audio_will_change(s)

    # 目标大小：计算码率
    v_kbps = None
    a_kbps = None
    if s["size_mode"] == "target" and not copy_v:
        a_kbps = 0
        if a and not copy_a:
            a_kbps = 96 if a_enc == "libopus" else 128
        elif a and copy_a:
            a_kbps = int((a.bitrate or 128000) / 1000)
        v_kbps = _target_video_kbps(s, prog, a_kbps)

    cmd += ["-map", "0:%d" % v.index]
    if a:
        cmd += ["-map", "0:%d" % a.index]

    # ---- 字幕
    subs_cmd = []
    if s["sub_mode"] == "keep" and p.s and t != "avi":
        k = 0
        dropped = 0
        for st in p.s[:16]:
            oc = sub_out_codec(t, st.codec)
            if oc is None:
                dropped += 1
                continue
            subs_cmd += ["-map", "0:%d" % st.index, "-c:s:%d" % k, oc]
            k += 1
        if dropped:
            notes.append("有 %d 条图片字幕无法放进 %s，已丢弃。" % (dropped, t.upper()))
    elif s["sub_mode"] == "keep" and p.s and t == "avi":
        notes.append("AVI 不支持内嵌字幕，字幕已去掉。")
    cmd += subs_cmd

    # ---- 视频编码参数
    if copy_v:
        cmd += ["-c:v", "copy"]
        if v.codec == "hevc" and t in ("mp4", "mov"):
            cmd += ["-tag:v", "hvc1"]
    else:
        vf = []
        if s["v_res"] and v.h > s["v_res"]:
            vf.append("scale=-2:%d" % s["v_res"])
        elif v.w % 2 or v.h % 2:
            vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        if s["v_fps"] and v.fps and v.fps > s["v_fps"] + 0.5:
            vf.append("fps=%d" % s["v_fps"])
        cmd += venc_args(vc, s, t, v_kbps)
        if vf:
            cmd += ["-vf", ",".join(vf)]

    # ---- 音频编码参数
    if a:
        if copy_a:
            cmd += ["-c:a", "copy"]
        else:
            cmd += audio_encode_args(a_enc, s, a.rate, a.ch, notes, a_kbps if (a_kbps and s["size_mode"] == "target") else None)

    if t in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]
    if start or dur is not None:
        cmd += ["-map_chapters", "-1"]
    cmd += ["-max_muxing_queue_size", "1024", out]
    mode = "%s画面 + %s声音" % ("复制" if copy_v else "重新编码", "复制" if copy_a else ("无" if not a else "重新编码"))
    if (copy_v or copy_a) and (start or dur is not None) and (s["trim_start"] or s["trim_end"]):
        notes.append("不重新编码时，剪切位置会靠近最近的关键帧，可能与填写的时间有零点几到几秒的误差。")
    return Plan(cmd, prog, notes, mode)


def plan_audio(ff, p, s, out, limit=None):
    s = norm(s)
    t = s["target"]
    notes = []
    if not p.a:
        raise BuildError("这个文件没有声音，无法转成音频。")
    a = pick_track(p.a, s["a_track"])
    start, dur = trim_range(p, s, limit)
    prog = dur if dur is not None else ((p.duration - start) if p.duration else None)
    cmd = base_cmd(ff)
    if start:
        cmd += ["-ss", ff_time(start)]
    cmd += ["-i", p.path]
    if dur is not None:
        cmd += ["-t", ff_time(dur)]
    cmd += ["-map", "0:%d" % a.index, "-vn", "-sn", "-dn"]

    copy_a = a.codec in A_COPY.get(t, ()) and not audio_will_change(s) and not s["force_encode"]
    mode = "原样提取（不重新压缩）" if copy_a else "重新编码"
    if copy_a:
        cmd += ["-c:a", "copy"]
    elif t in ("mp3", "m4a", "ogg", "opus"):
        enc = {"mp3": "libmp3lame", "m4a": "aac", "ogg": "libvorbis", "opus": "libopus"}[t]
        kbps = None
        if s["size_mode"] == "target":
            kbps = _lossy_kbps(enc, s, prog, notes)
        cmd += audio_encode_args(enc, s, a.rate, a.ch, notes, kbps)
    elif t == "wav":
        codec = {0: "pcm_s16le", 16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_f32le"}.get(s["a_bits"], "pcm_s16le")
        cmd += ["-c:a", codec]
        rate = pick_rate(s, "wav", a.rate, notes)
        if rate:
            cmd += ["-ar", str(rate)]
        ch = channel_count(s, "wav", a.ch)
        if ch:
            cmd += ["-ac", str(ch)]
        flt = audio_filters(s, a.ch)
        if flt:
            cmd += ["-af", ",".join(flt)]
        cmd += ["-rf64", "auto"]
    elif t == "flac":
        cmd += ["-c:a", "flac", "-compression_level", "5"]
        if s["a_bits"] == 16:
            cmd += ["-sample_fmt", "s16"]
        elif s["a_bits"] in (24, 32):
            cmd += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
        rate = pick_rate(s, "flac", a.rate, notes)
        if rate:
            cmd += ["-ar", str(rate)]
        ch = channel_count(s, "flac", a.ch)
        if ch:
            cmd += ["-ac", str(ch)]
        flt = audio_filters(s, a.ch)
        if flt:
            cmd += ["-af", ",".join(flt)]
    else:
        raise BuildError("不支持的音频格式：%s" % t)
    cmd += [out]
    if copy_a and (s["trim_start"] or s["trim_end"]):
        notes.append("原样提取时，剪切位置可能有极小误差。")
    return Plan(cmd, prog, notes, mode)


def asr_settings(s):
    t = norm(s)
    t.update(target="wav", a_rate=16000, a_bits=16, size_mode="quality", a_bitrate=0)
    if t["a_channels"] not in ("left", "right"):
        t["a_channels"] = "mono"
    return t


def plan_asr_wav(ff, p, s, out_wav, limit=None):
    return plan_audio(ff, p, asr_settings(s), out_wav, limit)


def srt_tracks(p, s):
    """返回要导出的 [(序号, Stream)]。"""
    s = norm(s)
    if not p.s:
        raise BuildError("这个视频没有内嵌字幕。")
    n = s["sub_track"]
    if n == 0:
        chosen = list(enumerate(p.s, 1))
    else:
        if n > len(p.s):
            raise BuildError("没有第 %d 条字幕（共 %d 条）。" % (n, len(p.s)))
        chosen = [(n, p.s[n - 1])]
    texts = [(i, st) for i, st in chosen if st.text]
    if not texts:
        raise BuildError("所选字幕是图片格式（如蓝光/DVD 字幕），无法直接转成文字 SRT。")
    return texts


def build_srt_cmd(ff, p, st, out):
    return Plan(base_cmd(ff, progress=False) + ["-i", p.path, "-map", "0:%d" % st.index, "-c:s", "srt", out],
                None, [], "字幕转换")


def plan_merge(ff, probes, s, out):
    """把多个文件按顺序合成一个（一律重新编码）。"""
    s = norm(s)
    t = s["target"]
    notes = ["合并时一律重新编码，用时较长；单个文件的单独设置与剪切不生效。"]
    if len(probes) < 2:
        raise BuildError("合并至少需要两个文件。")
    if t in SPECIAL_TARGETS or t == "gif":
        raise BuildError("合并只支持常规视频或音频格式，请重新选择“转换成”。")
    durs = [x.duration for x in probes]
    total = sum(durs) if all(durs) else None
    cmd = base_cmd(ff)
    for x in probes:
        cmd += ["-i", x.path]
    n = len(probes)
    graph = []
    extra_inputs = []
    kind = target_kind(t)
    aenc = A_DEFAULT_ENC.get(t)

    if kind == "video":
        for x in probes:
            if not x.v:
                raise BuildError("“%s”没有画面，不能合并成视频。请改选音频格式，或移除该文件。" % os.path.basename(x.path))
        f = probes[0].v
        W, H = f.w, f.h
        if s["v_res"] and H > s["v_res"]:
            W = int(round(W * s["v_res"] / float(H)))
            H = s["v_res"]
        W, H = W - W % 2, H - H % 2
        fps = f.fps or 25.0
        if s["v_fps"]:
            fps = min(fps, s["v_fps"])
        fps = min(fps, 60.0)
        parts = []
        for i, x in enumerate(probes):
            graph.append("[%d:%d]scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,"
                         "setsar=1,fps=%.3f,format=yuv420p[v%d]" % (i, x.v.index, W, H, W, H, fps, i))
            if x.a:
                a = pick_track(x.a, 1)
                graph.append("[%d:%d]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a%d]" % (i, a.index, i))
            else:
                k = n + len(extra_inputs)
                extra_inputs.append(["-f", "lavfi", "-t", "%.3f" % (x.duration or 1), "-i",
                                     "anullsrc=channel_layout=stereo:sample_rate=48000"])
                graph.append("[%d:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a%d]" % (k, i))
                notes.append("“%s”没有声音，已补上静音。" % os.path.basename(x.path))
            parts.append("[v%d][a%d]" % (i, i))
        graph.append("%sconcat=n=%d:v=1:a=1[vo][ao]" % ("".join(parts), n))
    else:
        for x in probes:
            if not x.a:
                raise BuildError("“%s”没有声音，不能合并成音频。请移除该文件。" % os.path.basename(x.path))
        parts = []
        for i, x in enumerate(probes):
            a = pick_track(x.a, 1)
            graph.append("[%d:%d]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a%d]" % (i, a.index, i))
            parts.append("[a%d]" % i)
        graph.append("%sconcat=n=%d:v=0:a=1[ao]" % ("".join(parts), n))

    # 额外输入（静音源）必须放在 -i 之后、滤镜之前
    for e in extra_inputs:
        cmd += e

    flt = audio_filters(s, 2)
    if flt:
        graph.append("[ao]%s[ao2]" % ",".join(flt))
        amap = "[ao2]"
    else:
        amap = "[ao]"
    cmd += ["-filter_complex", ";".join(graph)]

    if kind == "video":
        cmd += ["-map", "[vo]", "-map", amap]
        a_kbps = (96 if aenc == "libopus" else 128)
        v_kbps = _target_video_kbps(s, total, a_kbps) if s["size_mode"] == "target" else None
        vc = s["v_codec"]
        if vc in ("auto", "copy") or vc not in V_ENC_ALLOWED[t]:
            vc = V_DEFAULT[t]
        cmd += venc_args(vc, s, t, v_kbps)
        cmd += audio_encode_args(aenc, s, 48000, 2, notes, a_kbps if v_kbps else None)
        if t in ("mp4", "mov"):
            cmd += ["-movflags", "+faststart"]
        cmd += ["-max_muxing_queue_size", "1024"]
    else:
        cmd += ["-map", amap]
        cmd += _audio_only_encode(t, s, notes, total)
    cmd += [out]
    return Plan(cmd, total, notes, "合并（重新编码）")


def _audio_only_encode(t, s, notes, dur):
    """合并音频时使用的编码参数（输入已统一为 48kHz 立体声）。"""
    if t in ("mp3", "m4a", "ogg", "opus"):
        enc = {"mp3": "libmp3lame", "m4a": "aac", "ogg": "libvorbis", "opus": "libopus"}[t]
        kbps = _lossy_kbps(enc, s, dur, notes) if s["size_mode"] == "target" else None
        return audio_encode_args(enc, s, 48000, 2, notes, kbps)
    if t == "wav":
        codec = {0: "pcm_s16le", 16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_f32le"}.get(s["a_bits"], "pcm_s16le")
        a = ["-c:a", codec]
        if s["a_rate"]:
            a += ["-ar", str(s["a_rate"])]
        ch = channel_count(s, "wav", 2)
        if ch:
            a += ["-ac", str(ch)]
        return a + ["-rf64", "auto"]
    a = ["-c:a", "flac", "-compression_level", "5"]
    if s["a_bits"] == 16:
        a += ["-sample_fmt", "s16"]
    if s["a_rate"]:
        a += ["-ar", str(s["a_rate"])]
    ch = channel_count(s, "flac", 2)
    if ch:
        a += ["-ac", str(ch)]
    return a


def plan_for(ff, p, s, out, limit=None):
    """按目标类型选择计划函数（txt/srt 不走这里）。"""
    k = target_kind(norm(s)["target"])
    if k == "video":
        return plan_video(ff, p, s, out, limit)
    if k == "audio":
        return plan_audio(ff, p, s, out, limit)
    raise BuildError("此目标没有单条命令。")


def find_sv_files(exe_path):
    """根据 SenseVoice 程序位置，自动找同级 gguf 文件夹里的模型和 VAD 文件。返回 (模型路径, VAD路径)，找不到为空串。"""
    model = vad = ""
    try:
        d = os.path.dirname(os.path.abspath(exe_path))
        dirs = [os.path.join(d, "gguf"), d, os.path.join(os.path.dirname(d), "gguf")]
        for cd in dirs:
            if not os.path.isdir(cd):
                continue
            files = sorted(f for f in os.listdir(cd) if f.lower().endswith(".gguf"))
            if not model:
                for pat in ("f16", "q8", ""):
                    hit = [f for f in files if f.lower().startswith("sensevoice") and pat in f.lower()]
                    if hit:
                        model = os.path.join(cd, hit[0])
                        break
            if not vad:
                hit = [f for f in files if "vad" in f.lower()]
                if hit:
                    vad = os.path.join(cd, hit[0])
    except Exception:
        pass
    return model, vad
