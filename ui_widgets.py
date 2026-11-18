# -*- coding: utf-8 -*-
"""界面部件：下拉框、设置面板、文件行、弹窗。"""
import os
import tkinter as tk

import customtkinter as ctk

import core
import diag

log = diag.get_logger("界面")

FAMILY = "Microsoft YaHei UI"
MONO = "Consolas"

C_ACCENT = ("#2563eb", "#3b82f6")
C_ACCENT_HOVER = ("#1d4ed8", "#2563eb")
C_WARN = ("#ea580c", "#f97316")
C_WARN_HOVER = ("#c2410c", "#ea580c")
C_OK = ("#16a34a", "#4ade80")
C_ERR = ("#dc2626", "#f87171")
C_MUTED = ("#6b7280", "#9ca3af")
C_CARD = ("#ffffff", "#1f2430")
C_CARD_BORDER = ("#e5e7eb", "#2f3542")
C_PANEL = ("#f3f4f6", "#171a22")


def F(size=13, bold=False):
    return ctk.CTkFont(family=FAMILY, size=size, weight="bold" if bold else "normal")


def FM(size=11):
    return ctk.CTkFont(family=MONO, size=size)


def chk_style():
    return dict(fg_color=C_ACCENT, hover_color=C_ACCENT_HOVER, border_color=("gray60", "gray45"), border_width=2,
                checkbox_width=20, checkbox_height=20, corner_radius=5, font=F(12))


def ellipsize(text, n):
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------- 下拉框（值与显示文字分开）
class Opt(ctk.CTkOptionMenu):
    def __init__(self, master, options, command=None, width=190):
        self.fc_opts = list(options)
        self.fc_cmd = command
        super().__init__(master, values=[l for _, l in self.fc_opts] or [""], command=self._picked, width=width,
                         height=28, font=F(12), dropdown_font=F(12), dynamic_resizing=False, anchor="w",
                         fg_color=("#ffffff", "#262b38"), button_color=("#e5e7eb", "#323847"),
                         button_hover_color=("#d1d5db", "#3d4456"), text_color=("gray10", "gray92"),
                         dropdown_fg_color=("#ffffff", "#262b38"), dropdown_hover_color=("#e5e7eb", "#323847"),
                         dropdown_text_color=("gray10", "gray92"))
        if self.fc_opts:
            self.set(self.fc_opts[0][1])

    def _picked(self, _label):
        if self.fc_cmd:
            self.fc_cmd(self.value)

    @property
    def value(self):
        cur = self.get()
        for v, l in self.fc_opts:
            if l == cur:
                return v
        return self.fc_opts[0][0] if self.fc_opts else None

    def set_value(self, v):
        for val, l in self.fc_opts:
            if val == v:
                self.set(l)
                return True
        if self.fc_opts:
            self.set(self.fc_opts[0][1])
        return False

    def set_options(self, opts):
        cur = self.value
        self.fc_opts = list(opts)
        self.configure(values=[l for _, l in opts] or [""])
        self.set_value(cur)


# ---------------------------------------------------------------- 设置面板
OPT_KEYS = ["v_res", "v_fps", "v_codec", "v_speed", "gif_width", "gif_fps", "sub_mode", "a_mode", "a_track",
            "a_rate", "a_channels", "a_bits", "a_bitrate", "a_volume", "size_mode", "sub_track"]
ENTRY_KEYS = ["trim_start", "trim_end", "a_gain", "target_mb"]

SECTIONS = [
    ("剪切", ["trim_start", "trim_end"]),
    ("画面", ["v_res", "v_fps", "v_codec", "v_speed"]),
    ("动图", ["gif_width", "gif_fps"]),
    ("字幕", ["sub_mode", "sub_track"]),
    ("声音", ["a_mode", "a_track", "a_rate", "a_channels", "a_bits", "a_bitrate", "a_volume", "a_gain"]),
    ("文件大小", ["size_mode", "target_mb"]),
    ("语音识别", ["sv_status"]),
]

ROW_LABELS = {
    "trim_start": "开始时间", "trim_end": "结束时间", "v_res": "画面大小", "v_fps": "帧率", "v_codec": "视频编码",
    "v_speed": "编码速度", "gif_width": "宽度", "gif_fps": "帧率", "sub_mode": "内嵌字幕", "sub_track": "字幕轨",
    "a_mode": "声音处理", "a_track": "音轨", "a_rate": "采样率", "a_channels": "声道", "a_bits": "位深",
    "a_bitrate": "码率", "a_volume": "音量", "a_gain": "增减 (dB)", "size_mode": "压缩方式", "target_mb": "目标大小 (MB)",
    "sv_status": "SenseVoice",
}


class SettingsPanel(ctk.CTkFrame):
    def __init__(self, master, app, settings, on_change, probe=None, show_trim=False, preview_fn=None):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.on_change = on_change
        self.probe = probe
        self.show_trim = show_trim
        self.preview_fn = preview_fn
        self.cur = core.norm(settings)
        self.cur["preset"] = (settings or {}).get("preset", "")
        self.loading = False
        self._pv_job = None
        self.w = {}
        self.rows = {}
        self.hdrs = {}
        self.vars = {}
        self.adv_open = bool(app.cfg.get("adv_open", False)) if hasattr(app, "cfg") else False
        self._build()
        self.set(self.cur)

    # ----- 构建
    def _label(self, parent, text, width=92):
        return ctk.CTkLabel(parent, text=text, width=width, anchor="w", font=F(12))

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x")
        top.grid_columnconfigure(1, weight=1)
        self._label(top, "转换成").grid(row=0, column=0, sticky="w", pady=4)
        self.w["target"] = Opt(top, core.TARGET_LABELS, self._target_changed, width=210)
        self.w["target"].grid(row=0, column=1, sticky="ew", pady=4)
        self._label(top, "预设").grid(row=1, column=0, sticky="w", pady=4)
        self.w["preset"] = Opt(top, [("", core.CUSTOM_PRESET)], self._preset_changed, width=210)
        self.w["preset"].grid(row=1, column=1, sticky="ew", pady=4)
        pb = ctk.CTkFrame(top, fg_color="transparent")
        pb.grid(row=2, column=1, sticky="w")
        ctk.CTkButton(pb, text="保存为我的预设", width=104, height=24, font=F(11), fg_color="transparent",
                      border_width=1, text_color=("gray20", "gray85"), border_color=C_CARD_BORDER,
                      command=self._save_preset).pack(side="left", padx=(0, 6))
        ctk.CTkButton(pb, text="删除所选预设", width=92, height=24, font=F(11), fg_color="transparent",
                      border_width=1, text_color=("gray20", "gray85"), border_color=C_CARD_BORDER,
                      command=self._delete_preset).pack(side="left")
        self.q_label = self._label(top, "质量")
        self.q_label.grid(row=3, column=0, sticky="w", pady=6)
        self.q_seg = ctk.CTkSegmentedButton(top, values=[l for _, l in core.QUALITY_LABELS], command=self._quality_changed,
                                            font=F(12), height=28, selected_color=C_ACCENT, selected_hover_color=C_ACCENT_HOVER,
                                            fg_color=("#e5e7eb", "#2b3040"), unselected_color=("#e5e7eb", "#2b3040"),
                                            unselected_hover_color=("#d1d5db", "#3d4456"), text_color=("gray10", "gray92"))
        self.q_seg.grid(row=3, column=1, sticky="ew", pady=6)

        self.adv_btn = ctk.CTkButton(self, text="", height=30, anchor="w", fg_color="transparent",
                                     text_color=("gray15", "gray90"), hover_color=C_PANEL, font=F(13, True),
                                     command=self._toggle_adv)
        self.adv_btn.pack(fill="x", pady=(6, 0))
        self.adv = ctk.CTkFrame(self, fg_color="transparent")
        self.rows_frame = ctk.CTkFrame(self.adv, fg_color="transparent")
        self.rows_frame.pack(fill="x")

        for title, keys in SECTIONS:
            h = ctk.CTkLabel(self.rows_frame, text=title, anchor="w", font=F(11, True), text_color=C_MUTED)
            self.hdrs[title] = h
            for k in keys:
                self._make_row(k)

        self.hint = ctk.CTkLabel(self.adv, text="", anchor="w", justify="left", font=F(11), text_color=C_MUTED, wraplength=300)
        self.hint.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(self.adv, text="实际会执行的转换命令", anchor="w", font=F(11, True), text_color=C_MUTED).pack(fill="x", pady=(8, 2))
        self.pv = ctk.CTkTextbox(self.adv, height=96, font=FM(10), wrap="word", fg_color=C_CARD, border_width=1,
                                 border_color=C_CARD_BORDER)
        self.pv.pack(fill="x")
        self.pv.configure(state="disabled")
        self._update_adv_button()

    def _make_row(self, key):
        f = ctk.CTkFrame(self.rows_frame, fg_color="transparent")
        self._label(f, ROW_LABELS[key]).pack(side="left")
        if key in ENTRY_KEYS:
            var = tk.StringVar()
            e = ctk.CTkEntry(f, textvariable=var, height=28, font=F(12), width=120, fg_color=C_CARD, border_color=C_CARD_BORDER,
                             placeholder_text={"trim_start": "如 0:30，留空=从头", "trim_end": "如 1:20，留空=到尾"}.get(key, ""))
            e.pack(side="left", fill="x", expand=True)
            var.trace_add("write", lambda *a, k=key: self._entry_changed(k))
            self.vars[key] = var
            self.w[key] = e
        elif key == "sv_status":
            self.sv_label = ctk.CTkLabel(f, text="", anchor="w", font=F(12))
            self.sv_label.pack(side="left")
            ctk.CTkButton(f, text="设置路径", width=70, height=26, font=F(12), command=self.app.open_sv_dialog).pack(side="right")
            self.w[key] = self.sv_label
        else:
            opts = self._options_for(key)
            o = Opt(f, opts, lambda v, k=key: self._opt_changed(k, v), width=190)
            o.pack(side="left", fill="x", expand=True)
            self.w[key] = o
        self.rows[key] = f

    def _options_for(self, key):
        c = core
        table = {
            "v_res": c.RES_LABELS, "v_fps": c.FPS_LABELS, "v_codec": c.v_codec_options("mp4"),
            "v_speed": c.SPEED_LABELS, "gif_width": c.GIFW_LABELS, "gif_fps": c.GIFFPS_LABELS,
            "sub_mode": c.SUB_LABELS, "a_mode": c.AMODE_LABELS, "a_rate": c.RATE_LABELS,
            "a_channels": c.CH_LABELS, "a_bits": c.BITS_LABELS, "a_bitrate": c.BR_LABELS,
            "a_volume": c.VOL_LABELS, "size_mode": [("quality", "按质量"), ("target", "按目标大小")],
        }
        if key == "a_track":
            return self._track_options(False)
        if key == "sub_track":
            return self._track_options(True)
        return table[key]

    def _track_options(self, sub):
        p = self.probe
        if sub:
            if p and p.s:
                return [(0, "全部字幕")] + [(i, st.label(i)) for i, st in enumerate(p.s, 1)]
            return [(0, "全部字幕")] + [(i, "第%d条" % i) for i in range(1, 9)]
        if p and p.a:
            return [(i, st.label(i)) for i, st in enumerate(p.a, 1)]
        return [(i, "第%d条%s" % (i, "（默认）" if i == 1 else "")) for i in range(1, 9)]

    def set_probe(self, probe):
        self.probe = probe
        self.w["a_track"].set_options(self._track_options(False))
        self.w["sub_track"].set_options(self._track_options(True))

    # ----- 取值
    def get(self):
        d = dict(self.cur)
        return d

    def set(self, settings, notify=False):
        self.loading = True
        try:
            keep_preset = settings.get("preset", "") if isinstance(settings, dict) else ""
            self.cur = core.norm(settings)
            self.cur["preset"] = keep_preset
            c = self.cur
            self.w["target"].set_value(c["target"])
            self._refresh_codec_options()
            for k in OPT_KEYS:
                self.w[k].set_value(c[k])
            for k in ENTRY_KEYS:
                v = c[k]
                self.vars[k].set("" if v in ("", None) else (("%g" % v) if isinstance(v, float) else str(v)))
            self.q_seg.set(dict(core.QUALITY_LABELS).get(c["quality"], "标准"))
            self.refresh_presets()
            self.w["preset"].set_value(c["preset"])
        finally:
            self.loading = False
        self._relayout()
        self._schedule_preview()
        if notify:
            self.on_change(self.get())

    # ----- 事件
    def _changed(self, keep_preset=False):
        if self.loading:
            return
        if not keep_preset:
            self.cur["preset"] = ""
            self.w["preset"].set_value("")
        self._relayout()
        self._schedule_preview()
        self.on_change(self.get())

    def _refresh_codec_options(self):
        t = self.cur["target"]
        opts = core.v_codec_options(t) or [("auto", core.V_ENC_LABELS["auto"])]
        self.w["v_codec"].set_options(opts)
        if self.cur["v_codec"] not in [v for v, _ in opts]:
            self.cur["v_codec"] = "auto"
        self.w["v_codec"].set_value(self.cur["v_codec"])

    def _target_changed(self, v):
        diag.trail("选择目标格式：%s" % v)
        self.cur["target"] = v
        self._refresh_codec_options()
        self._changed()

    def _quality_changed(self, label):
        for k, l in core.QUALITY_LABELS:
            if l == label:
                self.cur["quality"] = k
        diag.trail("选择质量：%s" % label)
        self._changed()

    def _opt_changed(self, key, v):
        self.cur[key] = v
        diag.trail("高级设置 %s=%s" % (key, v))
        self._changed()

    def _entry_changed(self, key):
        if self.loading:
            return
        t = self.vars[key].get().strip()
        if key in ("trim_start", "trim_end"):
            self.cur[key] = t
        else:
            try:
                self.cur[key] = float(t)
            except ValueError:
                return
        self._changed()

    def _preset_changed(self, value):
        if self.loading or not value:
            if not value:
                self.cur["preset"] = ""
            return
        if value.startswith("u:"):
            name = value[2:]
            s = core.preset_settings(name, self.app.user_presets)
        else:
            name = value
            s = core.preset_settings(name)
        if s is None:
            return
        s["trim_start"], s["trim_end"] = self.cur["trim_start"], self.cur["trim_end"]
        s["a_track"] = self.cur["a_track"]
        s["preset"] = value
        diag.trail("选择预设：%s" % name)
        self.set(s, notify=True)

    def refresh_presets(self):
        opts = [("", core.CUSTOM_PRESET)] + [(n, n) for n in core.BUILTIN_PRESETS]
        opts += [("u:" + n, "★ " + n) for n in self.app.user_presets]
        self.w["preset"].set_options(opts)

    def _save_preset(self):
        self.app.save_current_as_preset(self)

    def _delete_preset(self):
        self.app.delete_preset(self, self.w["preset"].value)

    def _toggle_adv(self):
        self.adv_open = not self.adv_open
        self.app.cfg["adv_open"] = self.adv_open
        self.app.save_cfg_soon()
        self._update_adv_button()
        diag.trail("%s高级设置" % ("展开" if self.adv_open else "收起"))

    def _update_adv_button(self):
        self.adv_btn.configure(text=("▾  高级设置" if self.adv_open else "▸  高级设置（采样率、声道、画面、剪切等）"))
        if self.adv_open:
            self.adv.pack(fill="x", pady=(2, 0))
        else:
            self.adv.pack_forget()

    # ----- 显示逻辑
    def visible_keys(self):
        c = self.cur
        t = c["target"]
        kind = core.target_kind(t)
        v = set()
        lossy = t in ("mp3", "m4a", "ogg", "opus")
        vid = kind == "video" and t != "gif"
        if self.show_trim and t != "srt":
            v |= {"trim_start", "trim_end"}
        if vid:
            if c["v_codec"] == "copy":
                v |= {"v_codec"}
            else:
                v |= {"v_res", "v_fps", "v_codec", "v_speed"}
            if t != "avi":
                v.add("sub_mode")
            v |= {"a_mode"}
            if c["a_mode"] == "copy":
                v |= {"a_track"}
            elif c["a_mode"] != "none":
                v |= {"a_track", "a_rate", "a_channels", "a_bitrate", "a_volume"}
        if t == "gif":
            v |= {"gif_width", "gif_fps"}
        if kind == "audio":
            v |= {"a_track", "a_rate", "a_channels", "a_volume"}
            if t in ("wav", "flac"):
                v.add("a_bits")
            if lossy:
                v.add("a_bitrate")
        if t == "txt":
            v |= {"a_track", "a_volume", "sv_status"}
        if t == "srt":
            v.add("sub_track")
        if vid or lossy:
            v.add("size_mode")
            if c["size_mode"] == "target":
                v.add("target_mb")
        if "a_volume" in v and c["a_volume"] == "gain":
            v.add("a_gain")
        return v

    def _relayout(self):
        vis = self.visible_keys()
        for title, keys in SECTIONS:
            self.hdrs[title].pack_forget()
            for k in keys:
                self.rows[k].pack_forget()
        for title, keys in SECTIONS:
            show = [k for k in keys if k in vis]
            if not show:
                continue
            self.hdrs[title].pack(fill="x", pady=(8, 0))
            for k in show:
                self.rows[k].pack(fill="x", pady=2)
        c = self.cur
        t = c["target"]
        lossy = t in ("mp3", "m4a", "ogg", "opus") or (core.target_kind(t) == "video" and t != "gif")
        show_q = lossy and c["size_mode"] != "target" and not (core.target_kind(t) == "video" and c["v_codec"] == "copy")
        if show_q:
            self.q_label.grid()
            self.q_seg.grid()
        else:
            self.q_label.grid_remove()
            self.q_seg.grid_remove()
        hints = []
        if t == "txt":
            hints.append("识别前会自动把声音转成 16kHz 单声道，无需另外设置。")
        if t == "srt":
            hints.append("只能提取文字字幕；蓝光/DVD 的图片字幕不能转成文字。")
        if self.show_trim and t != "srt":
            hints.append("时间可写 90、1:30 或 01:02:03。留空表示不剪切。")
        if t in ("mp4", "mov", "mkv", "webm", "avi") and c["v_codec"] == "auto":
            hints.append("“自动”：只换外壳时直接复制内容，几秒完成、画质不变。")
        self.hint.configure(text="\n".join(hints))
        self.refresh_sv()

    def refresh_sv(self):
        try:
            ok = self.app.sv_ready()
            self.sv_label.configure(text="已设置" if ok else "未设置",
                                    text_color=C_OK if ok else C_ERR)
        except Exception:
            pass

    # ----- 预览
    def _schedule_preview(self):
        if self._pv_job:
            try:
                self.after_cancel(self._pv_job)
            except Exception:
                pass
        self._pv_job = self.after(250, self.refresh_preview)

    def refresh_preview(self):
        self._pv_job = None
        try:
            if not self.adv_open or not self.preview_fn:
                return
            text = self.preview_fn(self.get())
        except Exception as e:
            text = "（预览出错：%s）" % e
        try:
            self.pv.configure(state="normal")
            self.pv.delete("1.0", "end")
            self.pv.insert("1.0", text)
            self.pv.configure(state="disabled")
        except Exception:
            pass


# ---------------------------------------------------------------- 文件行
STATUS_COLORS = {"等待": C_MUTED, "探测中": C_MUTED, "转换中": C_ACCENT, "完成": C_OK, "失败": C_ERR,
                 "已取消": C_WARN, "无法读取": C_ERR}


class FileRow(ctk.CTkFrame):
    def __init__(self, master, app, item):
        super().__init__(master, corner_radius=10, fg_color=C_CARD, border_width=1, border_color=C_CARD_BORDER)
        self.app = app
        self.item = item
        self.grid_columnconfigure(1, weight=1)
        self.idx = ctk.CTkLabel(self, text="", width=28, font=F(12), text_color=C_MUTED)
        self.idx.grid(row=0, column=0, rowspan=2, padx=(8, 0))
        self.name = ctk.CTkLabel(self, text=ellipsize(item.name, 56), anchor="w", font=F(13, True))
        self.name.grid(row=0, column=1, sticky="ew", padx=(6, 6), pady=(8, 0))
        self.info = ctk.CTkLabel(self, text="正在读取文件信息…", anchor="w", justify="left", font=F(11), text_color=C_MUTED, wraplength=420)
        self.info.grid(row=1, column=1, sticky="ew", padx=(6, 6), pady=(0, 2))
        self.status = ctk.CTkLabel(self, text="", anchor="e", font=F(12))
        self.status.grid(row=0, column=2, sticky="e", padx=(0, 6), pady=(8, 0))
        self.bar = ctk.CTkProgressBar(self, height=6)
        self.bar.set(0)
        self.msg = ctk.CTkLabel(self, text="", anchor="w", justify="left", font=F(11), wraplength=420)
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=0, column=3, rowspan=2, padx=(0, 8))
        self.btns = {}
        for key, text, w, cmd in (("open", "打开", 40, lambda: app.open_result(item)),
                                  ("up", "↑", 28, lambda: app.move_item(item, -1)),
                                  ("down", "↓", 28, lambda: app.move_item(item, 1)),
                                  ("cfg", "设置", 44, lambda: app.edit_item_settings(item)),
                                  ("rm", "×", 28, lambda: app.remove_item(item))):
            b = ctk.CTkButton(btns, text=text, width=w, height=26, font=F(12), fg_color="transparent",
                              text_color=("gray25", "gray80"), hover_color=C_PANEL, command=cmd)
            b.pack(side="left", padx=1)
            self.btns[key] = b
        self.btns["open"].pack_forget()
        self._bar_mode = "determinate"
        self._bar_shown = False
        for w in (self, self.name, self.info, self.idx, self.status):
            w.bind("<Button-1>", lambda e: app.select_item(item))
        self._wrap = 0
        self.bind("<Configure>", self._on_resize)
        self.refresh()

    def _on_resize(self, e):
        try:
            wl = max(180, e.width - 230)
            if abs(wl - self._wrap) > 10:
                self._wrap = wl
                self.info.configure(wraplength=wl)
                self.msg.configure(wraplength=max(240, e.width - 110))
        except Exception:
            pass

    def set_index(self, i):
        self.idx.configure(text=str(i))

    def set_selected(self, on):
        self.configure(border_color=C_ACCENT if on else C_CARD_BORDER, border_width=2 if on else 1)

    def set_locked(self, locked, merge=False):
        st = "disabled" if locked else "normal"
        for k in ("up", "down", "rm"):
            self.btns[k].configure(state=st)
        self.btns["cfg"].configure(state="disabled" if (locked or merge) else "normal")

    def show_bar(self, show):
        if show and not self._bar_shown:
            self.bar.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(6, 6), pady=(0, 8))
            self._bar_shown = True
        elif not show and self._bar_shown:
            self.bar.grid_forget()
            if self._bar_mode == "indeterminate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self._bar_mode = "determinate"
            self._bar_shown = False

    def set_progress(self, pct):
        self.show_bar(True)
        if pct is None:
            if self._bar_mode != "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start()
                self._bar_mode = "indeterminate"
        else:
            if self._bar_mode != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self._bar_mode = "determinate"
            self.bar.set(max(0.0, min(1.0, pct / 100.0)))

    def refresh(self):
        it = self.item
        p = it.probe
        if p:
            tag = "（有单独设置）" if it.settings else ""
            self.info.configure(text=p.info() + tag, text_color=C_MUTED)
        elif it.probe_error:
            self.info.configure(text="", text_color=C_ERR)
        st = it.status
        self.status.configure(text={"探测中": "读取中…", "等待": "等待中"}.get(st, st), text_color=STATUS_COLORS.get(st, C_MUTED))
        if st == "转换中":
            self.set_progress(it.pct if it.pct is not None else None)
        elif st == "完成":
            self.set_progress(100)
            self.after(600, lambda: self.show_bar(False) if self.item.status == "完成" else None)
        else:
            self.show_bar(False)
        text, color = "", C_MUTED
        if st in ("失败", "无法读取") and (it.message or it.probe_error):
            text, color = (it.message or it.probe_error), C_ERR
        elif st == "转换中" and it.message:
            text = it.message
        elif st == "完成":
            outs = ", ".join(os.path.basename(o) for o in it.out_paths)
            text = "已保存：" + outs + ("；耗时 " + core.fmt_hms(it.elapsed) if it.elapsed else "")
            if it.notes:
                text += "\n提示：" + "；".join(it.notes)
            color = C_OK
        if text:
            self.msg.configure(text=text, text_color=color)
            self.msg.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(6, 6), pady=(0, 8))
        else:
            self.msg.grid_forget()
        if st == "完成" and it.out_paths:
            self.btns["open"].pack(side="left", padx=1, before=self.btns["up"])
        else:
            self.btns["open"].pack_forget()
        self.btns["cfg"].configure(text_color=C_ACCENT if it.settings else ("gray25", "gray80"),
                                   font=F(12, bool(it.settings)))


# ---------------------------------------------------------------- 弹窗
def center_on(win, parent, w, h):
    try:
        parent.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        win.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))
    except Exception:
        win.geometry("%dx%d" % (w, h))


class TextDialog(ctk.CTkToplevel):
    """显示一段可全选复制的文字（用于剪贴板失败时的诊断报告、语音识别结果等）。"""

    def __init__(self, parent, title, intro, text, width=720, height=520):
        super().__init__(parent)
        self.title(title)
        center_on(self, parent, width, height)
        self.after(150, lambda: (self.lift(), self.focus_force()))
        ctk.CTkLabel(self, text=intro, anchor="w", justify="left", wraplength=width - 40, font=F(12)).pack(fill="x", padx=16, pady=(14, 6))
        self.box = ctk.CTkTextbox(self, font=F(12), wrap="word")
        self.box.pack(fill="both", expand=True, padx=16, pady=6)
        self.box.insert("1.0", text)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(4, 14))
        ctk.CTkButton(bar, text="全选", width=70, font=F(12), command=self._sel).pack(side="left")
        ctk.CTkButton(bar, text="复制", width=70, font=F(12), command=self._copy).pack(side="left", padx=8)
        ctk.CTkButton(bar, text="关闭", width=70, font=F(12), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=self.destroy).pack(side="right")
        self.box.focus_set()
        self._sel()

    def _sel(self):
        try:
            self.box._textbox.tag_add("sel", "1.0", "end-1c")
            self.box._textbox.focus_set()
        except Exception:
            pass

    def _copy(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self.box.get("1.0", "end-1c"))
        except Exception:
            pass
