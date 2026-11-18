# -*- coding: utf-8 -*-
"""主界面。"""
import contextlib
import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

import core
import diag
import power
import runner
from ui_widgets import (C_ACCENT, C_ACCENT_HOVER, C_CARD, C_CARD_BORDER, C_ERR, C_MUTED, C_OK, C_PANEL, C_WARN,
                        C_WARN_HOVER, F, FM, FileRow, Opt, SettingsPanel, TextDialog, center_on, chk_style, ellipsize)

log = diag.get_logger("界面")

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    class _Root(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self):
            super().__init__()
            self.dnd_ok = False
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_ok = True
            except Exception as e:
                log.warning("拖拽功能不可用：%s", e)
    HAS_DND = True
except Exception as _e:   # 库缺失
    _Root = None
    DND_FILES = None
    HAS_DND = False

MEDIA_EXTS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".ts", ".m2ts", ".mts", ".mpg",
              ".mpeg", ".3gp", ".3g2", ".rmvb", ".rm", ".vob", ".ogv", ".asf", ".f4v", ".mxf", ".mp3", ".m4a",
              ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma", ".amr", ".aiff", ".aif", ".ape", ".ac3",
              ".mka", ".m4b", ".caf", ".dts"}
MAX_FILES = 200

OUT_MODES = [("folder", "原文件旁的“转换结果”文件夹"), ("same", "与原文件同一文件夹"), ("custom", "自定义文件夹…")]


def open_path(path, select=False):
    try:
        if os.name == "nt":
            if select and os.path.isfile(path):
                subprocess.Popen('explorer /select,"%s"' % path)
            else:
                os.startfile(path if os.path.isdir(path) else os.path.dirname(path) if select else path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path if os.path.isdir(path) else os.path.dirname(path)])
        else:
            subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])
    except Exception as e:
        log.warning("打开失败：%s", e)


def open_file(path):
    try:
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        log.warning("打开失败：%s", e)


class App:
    def __init__(self, base_dir):
        self.base = base_dir
        self.data_dir = diag.data_dir() or base_dir
        self.cfg_path = os.path.join(self.data_dir, "config.json")
        self.cfg = self._load_cfg()
        self.user_presets = self.cfg.get("user_presets", {})
        self.ffmpeg = core.find_tool("ffmpeg")
        self.ffprobe = core.find_tool("ffprobe")
        self.items = []
        self.rows = {}
        self.selected = None
        self.q = queue.Queue()
        self.running = False
        self.mode = ""            # batch / trial
        self.ctx = None
        self.run_merge = False
        self.run_snap = None
        self.jump_item = None
        self.sched_at = None
        self._sched_job = None
        self._queue_job = None
        self.last_outs = []
        self._cfg_job = None
        self._toast_job = None
        self._sv_dlg = None
        self._sv_status = None
        self.tmp_dir = os.path.join(self.data_dir, "tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._clean_tmp()

        ctk.set_appearance_mode("system")
        self._apply_theme()
        self.root = _Root() if _Root else ctk.CTk()
        self.root.title("%s  v%s" % (diag.APP_NAME, diag.APP_VERSION))
        self.root.geometry("1100x780")
        self.root.minsize(960, 660)
        self._set_icon()
        diag.install_hooks(self.root)
        diag.set_crash_callback(lambda path: self.q.put(("crash", path)))
        diag.add_provider("当前状态", self._state_text)
        diag.add_provider("设置快照", self._config_text)
        diag.add_provider("文件列表", self._items_text)
        diag.add_provider("运行细节（暂停/排队/识别速度）", self._run_text)

        self.probe_q = queue.Queue()
        threading.Thread(target=self._probe_loop, daemon=True, name="探测线程").start()
        self._build()
        self._setup_dnd()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._pump)
        threading.Thread(target=self._get_ffver, daemon=True, name="版本线程").start()
        if not (self.ffmpeg and self.ffprobe):
            self.banner.configure(text="⚠ 找不到转换引擎（ffmpeg）。请确认程序文件夹里的 ffmpeg 文件夹没有被删除或被杀毒软件隔离。")
            self.banner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 6))
        diag.trail("程序启动")
        self._restore_queue()
        log.info("ffmpeg=%s ffprobe=%s 拖拽=%s", diag.redact(str(self.ffmpeg)), diag.redact(str(self.ffprobe)),
                 getattr(self.root, "dnd_ok", False))

    def _apply_theme(self):
        """把默认蓝色主题换成统一的强调色。失败就用默认蓝。"""
        try:
            p = os.path.join(os.path.dirname(ctk.__file__), "assets", "themes", "blue.json")
            with open(p, "r", encoding="utf-8") as f:
                t = f.read()
            for a, b in (("#3B8ED0", "#2563EB"), ("#1F6AA5", "#1D4ED8"), ("#36719F", "#1D4ED8"), ("#144870", "#1E40AF")):
                t = t.replace(a, b)
            tp = os.path.join(self.data_dir, "theme.json")
            with open(tp, "w", encoding="utf-8") as f:
                f.write(t)
            ctk.set_default_color_theme(tp)
        except Exception as e:
            log.warning("主题加载失败，使用默认：%s", e)
            ctk.set_default_color_theme("blue")

    # ------------------------------------------------------------ 配置
    def _load_cfg(self):
        d = {"settings": core.norm(None), "user_presets": {}, "out_mode": "folder", "out_custom": "",
             "low_priority": True, "open_after": True, "sv": {"exe": "", "model": "", "vad": ""}, "adv_open": False,
             "asr_simplified": True, "asr_tidy": True, "asr_split": True}
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k, v in loaded.items():
                d[k] = v
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("读取配置失败，使用默认设置：%s", e)
        return d

    def save_cfg_soon(self):
        if self._cfg_job:
            try:
                self.root.after_cancel(self._cfg_job)
            except Exception:
                pass
        self._cfg_job = self.root.after(600, self._save_cfg)

    def _save_cfg(self):
        self._cfg_job = None
        try:
            self.cfg["user_presets"] = self.user_presets
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.cfg_path)
        except Exception as e:
            log.warning("保存配置失败：%s", e)

    def _clean_tmp(self):
        try:
            import shutil
            shutil.rmtree(os.path.join(self.tmp_dir, "trial"), ignore_errors=True)
            runner.clean_resume(self.data_dir)
            for f in os.listdir(self.tmp_dir):
                p = os.path.join(self.tmp_dir, f)
                if os.path.isfile(p):
                    os.remove(p)
        except Exception:
            pass

    def _set_icon(self):
        try:
            if os.name == "nt":
                cands = [os.path.join(getattr(sys, "_MEIPASS", ""), "app.ico"), os.path.join(self.base, "app.ico")]
                for p in cands:
                    if os.path.isfile(p):
                        self.root.after(250, lambda p=p: self.root.iconbitmap(p))
                        break
        except Exception:
            pass

    # ------------------------------------------------------------ 界面搭建
    def _build(self):
        r = self.root
        r.grid_columnconfigure(0, weight=1)
        r.grid_rowconfigure(2, weight=1)
        # 顶栏
        self.hdr = ctk.CTkFrame(r, fg_color="transparent")
        self.hdr.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        self.hdr.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(self.hdr, text="格式转换", font=F(22, True)).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(self.hdr, text="v%s" % diag.APP_VERSION, font=F(12), text_color=C_MUTED).grid(row=0, column=1, sticky="sw", padx=(8, 0), pady=(0, 3))
        self.toast_lbl = ctk.CTkLabel(self.hdr, text="", font=F(12), text_color=C_OK, anchor="e")
        self.toast_lbl.grid(row=0, column=2, sticky="e", padx=10)
        ctk.CTkButton(self.hdr, text="设置", width=64, height=32, font=F(13), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), border_color=C_CARD_BORDER, command=self.open_sv_dialog
                      ).grid(row=0, column=3, padx=(0, 8))
        ctk.CTkButton(self.hdr, text="复制诊断报告", width=118, height=32, font=F(13, True), fg_color=C_WARN,
                      hover_color=C_WARN_HOVER, command=self.copy_diag).grid(row=0, column=4)
        self.banner = ctk.CTkLabel(r, text="", font=F(12), text_color="white", fg_color=C_ERR, corner_radius=8,
                                   anchor="w", justify="left", wraplength=980, height=34)

        # 主体
        body = ctk.CTkFrame(r, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=18)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        self._build_left(left)
        right = ctk.CTkScrollableFrame(body, width=350, fg_color=C_PANEL, corner_radius=12)
        right.grid(row=0, column=1, sticky="ns")
        self._build_right(right)

        # 底栏
        bottom = ctk.CTkFrame(r, fg_color="transparent")
        bottom.grid(row=3, column=0, sticky="ew", padx=18, pady=(10, 14))
        bottom.grid_columnconfigure(1, weight=1)
        self.sum_lbl = ctk.CTkLabel(bottom, text="", anchor="w", font=F(12), text_color=C_MUTED, width=330)
        self.sum_lbl.grid(row=0, column=0, sticky="w")
        self.total_bar = ctk.CTkProgressBar(bottom, height=8)
        self.total_bar.set(0)
        self.total_bar.grid(row=0, column=1, sticky="ew", padx=14)
        outline = dict(fg_color="transparent", border_width=1, text_color=("gray20", "gray85"), border_color=C_CARD_BORDER)
        self.sched_btn = ctk.CTkButton(bottom, text="定时开始", width=92, height=38, font=F(13), command=self.sched_clicked, **outline)
        self.sched_btn.grid(row=0, column=2, padx=(0, 8))
        self.trial_btn = ctk.CTkButton(bottom, text="试转前 30 秒", width=110, height=38, font=F(13), command=self.trial, **outline)
        self.trial_btn.grid(row=0, column=3, padx=(0, 8))
        self.start_btn = ctk.CTkButton(bottom, text="开始转换", width=130, height=38, font=F(14, True), fg_color=C_ACCENT,
                                       hover_color=C_ACCENT_HOVER, command=self.start)
        self.start_btn.grid(row=0, column=4)
        # 转换中才出现的控件
        self.pause_after_var = tk.BooleanVar(value=False)
        self.pause_after_chk = ctk.CTkCheckBox(bottom, text="当前文件完成后暂停", variable=self.pause_after_var,
                                               command=self.pause_after_toggled, **chk_style())
        self.pause_btn = ctk.CTkButton(bottom, text="暂停", width=92, height=38, font=F(14, True), fg_color=C_WARN,
                                       hover_color=C_WARN_HOVER, command=self.toggle_pause)
        self.cancel_btn = ctk.CTkButton(bottom, text="取消", width=110, height=38, font=F(14, True), fg_color=C_ERR,
                                        hover_color=("#b91c1c", "#dc2626"), command=self.cancel)
        self._refresh_summary()

    def _build_left(self, left):
        drop = ctk.CTkFrame(left, corner_radius=12, fg_color=C_CARD, border_width=2, border_color=C_CARD_BORDER)
        drop.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        drop.grid_columnconfigure(0, weight=1)
        self.drop_lbl = ctk.CTkLabel(drop, text="把视频、音频文件或文件夹拖到这里", font=F(14), text_color=C_MUTED)
        self.drop_lbl.grid(row=0, column=0, columnspan=3, pady=(14, 6))
        bar = ctk.CTkFrame(drop, fg_color="transparent")
        bar.grid(row=1, column=0, pady=(0, 14))
        self.add_btn = ctk.CTkButton(bar, text="添加文件", width=96, height=32, font=F(13), command=self.add_dialog)
        self.add_btn.pack(side="left", padx=4)
        self.addf_btn = ctk.CTkButton(bar, text="添加文件夹", width=96, height=32, font=F(13), fg_color="transparent",
                                      border_width=1, text_color=("gray20", "gray85"), border_color=C_CARD_BORDER,
                                      command=self.add_folder_dialog)
        self.addf_btn.pack(side="left", padx=4)
        self.clear_btn = ctk.CTkButton(bar, text="清空列表", width=84, height=32, font=F(13), fg_color="transparent",
                                       border_width=1, text_color=("gray20", "gray85"), border_color=C_CARD_BORDER,
                                       command=self.clear_items)
        self.clear_btn.pack(side="left", padx=4)
        self.drop_frame = drop
        self.list = ctk.CTkScrollableFrame(left, fg_color="transparent", corner_radius=0)
        self.list.grid(row=1, column=0, sticky="nsew")
        self.empty_lbl = ctk.CTkLabel(self.list, text="还没有文件。\n把文件拖进来，或点上面的“添加文件”。", font=F(13),
                                      text_color=C_MUTED, justify="center")
        self.empty_lbl.pack(pady=60)
        foot = ctk.CTkFrame(left, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.merge_var = tk.BooleanVar(value=False)
        self.merge_chk = ctk.CTkCheckBox(foot, text="合并成一个文件（按列表顺序，可用 ↑ ↓ 调整）", variable=self.merge_var,
                                         command=self._merge_toggled, **chk_style())
        self.merge_chk.pack(side="left")

    def _build_right(self, right):
        s = dict(self.cfg.get("settings") or {})
        self.panel = SettingsPanel(right, self, s, self._on_global_change, preview_fn=self._preview_global)
        self.panel.pack(fill="x", padx=12, pady=(12, 4))
        # 保存位置
        box = ctk.CTkFrame(right, fg_color="transparent")
        box.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(box, text="保存位置", anchor="w", font=F(13, True)).pack(fill="x")
        self.out_opt = Opt(box, OUT_MODES, self._out_changed, width=300)
        self.out_opt.pack(fill="x", pady=(4, 2))
        self.out_opt.set_value(self.cfg.get("out_mode", "folder"))
        self.out_lbl = ctk.CTkLabel(box, text="", anchor="w", font=F(11), text_color=C_MUTED, wraplength=300, justify="left")
        self.out_lbl.pack(fill="x")
        self._refresh_out_label()
        # 选项
        opts = ctk.CTkFrame(right, fg_color="transparent")
        opts.pack(fill="x", padx=12, pady=(10, 14))
        ctk.CTkLabel(opts, text="转换选项", anchor="w", font=F(13, True)).pack(fill="x")
        self.low_var = tk.BooleanVar(value=bool(self.cfg.get("low_priority", True)))
        self.open_var = tk.BooleanVar(value=bool(self.cfg.get("open_after", True)))
        self.shut_var = tk.BooleanVar(value=False)
        for text, var, key in (("转换时降低占用（电脑不卡，稍慢）", self.low_var, "low_priority"),
                               ("完成后自动打开保存文件夹", self.open_var, "open_after"),
                               ("全部完成后自动关机", self.shut_var, None)):
            ctk.CTkCheckBox(opts, text=text, variable=var, command=lambda v=var, k=key, t=text: self._opt_toggled(v, k, t),
                            **chk_style()).pack(anchor="w", pady=(7, 0))
        asr = ctk.CTkFrame(right, fg_color="transparent")
        asr.pack(fill="x", padx=12, pady=(0, 14))
        ctk.CTkLabel(asr, text="语音转文字的结果", anchor="w", font=F(13, True)).pack(fill="x")
        self.asr_vars = {}
        for text, key in (("转为简体中文", "asr_simplified"), ("去掉识别标记、整理多余空格", "asr_tidy"),
                          ("按句分行（遇到。！？换行）", "asr_split")):
            var = tk.BooleanVar(value=bool(self.cfg.get(key, True)))
            self.asr_vars[key] = var
            ctk.CTkCheckBox(asr, text=text, variable=var, command=lambda v=var, k=key, t=text: self._opt_toggled(v, k, t),
                            **chk_style()).pack(anchor="w", pady=(7, 0))

    def _setup_dnd(self):
        try:
            if getattr(self.root, "dnd_ok", False):
                self.root.drop_target_register(DND_FILES)
                self.root.dnd_bind("<<Drop>>", self._on_drop)
                self.root.dnd_bind("<<DropEnter>>", lambda e: self.drop_frame.configure(border_color=C_ACCENT))
                self.root.dnd_bind("<<DropLeave>>", lambda e: self.drop_frame.configure(border_color=C_CARD_BORDER))
            else:
                self.drop_lbl.configure(text="点下面的“添加文件”选择文件（拖拽功能在这台电脑上不可用）")
        except Exception as e:
            log.warning("注册拖拽失败：%s", e)

    def _on_drop(self, event):
        self.drop_frame.configure(border_color=C_CARD_BORDER)
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            paths = [event.data]
        diag.trail("拖入 %d 个文件/文件夹" % len(paths))
        self.add_paths(paths)
        return event.action

    # ------------------------------------------------------------ 小工具
    def toast(self, text, ms=4000, color=None):
        self.toast_lbl.configure(text=text, text_color=color or C_OK)
        if self._toast_job:
            try:
                self.root.after_cancel(self._toast_job)
            except Exception:
                pass
        self._toast_job = self.root.after(ms, lambda: self.toast_lbl.configure(text=""))

    def _opt_toggled(self, var, key, text):
        diag.trail("%s：%s" % (text, "开" if var.get() else "关"))
        if key:
            self.cfg[key] = bool(var.get())
            self.save_cfg_soon()

    def _out_changed(self, v):
        diag.trail("保存位置：%s" % v)
        if v == "custom":
            d = filedialog.askdirectory(title="选择保存文件夹", initialdir=self.cfg.get("out_custom") or None)
            if d:
                self.cfg["out_custom"] = os.path.normpath(d)
            elif not self.cfg.get("out_custom"):
                self.out_opt.set_value("folder")
                v = "folder"
        self.cfg["out_mode"] = v
        self._refresh_out_label()
        self.save_cfg_soon()

    def _refresh_out_label(self):
        m = self.cfg.get("out_mode", "folder")
        self.out_lbl.configure(text=("→ " + self.cfg.get("out_custom", "")) if m == "custom" else
                               "不会覆盖原文件；重名时自动加 (2)。")

    def _on_global_change(self, s):
        self.cfg["settings"] = dict(s)
        self.save_cfg_soon()

    # ------------------------------------------------------------ 文件管理
    def add_dialog(self):
        diag.trail("点击 添加文件")
        exts = " ".join("*" + e for e in sorted(MEDIA_EXTS))
        paths = filedialog.askopenfilenames(title="选择要转换的文件",
                                            filetypes=[("音视频文件", exts), ("所有文件", "*.*")])
        if paths:
            self.add_paths(list(paths))

    def add_folder_dialog(self):
        diag.trail("点击 添加文件夹")
        d = filedialog.askdirectory(title="选择文件夹（会添加里面所有音视频文件）")
        if d:
            self.add_paths([d])

    def _mut(self):
        """转换进行中修改文件列表时，和后台线程互斥。"""
        if self.ctx is not None and self.running:
            return self.ctx.lock
        return contextlib.nullcontext()

    def add_paths(self, paths):
        if self.running and self.mode == "batch" and self.run_merge:
            self.toast("正在合并，合并过程中不能添加文件", color=C_WARN)
            return
        existing = {os.path.normcase(os.path.abspath(i.path)) for i in self.items}
        new, skipped_dup, skipped_limit = [], 0, 0
        files = []
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                for dp, _dn, fn in os.walk(p):
                    for f in sorted(fn):
                        if os.path.splitext(f)[1].lower() in MEDIA_EXTS:
                            files.append(os.path.join(dp, f))
            elif os.path.isfile(p):
                files.append(p)
        for p in files:
            key = os.path.normcase(os.path.abspath(p))
            if key in existing:
                skipped_dup += 1
                continue
            if len(self.items) + len(new) >= MAX_FILES:
                skipped_limit += 1
                continue
            existing.add(key)
            new.append(p)
        live = self.running and self.mode == "batch"
        snap = core.norm(self.panel.get()) if live else None
        for p in new:
            it = runner.Item(p)
            if live:
                it.in_batch, it.snap = True, snap      # 转换中途添加：用添加这一刻的设置，排在队尾自动转
            with self._mut():
                self.items.append(it)
            self._make_row(it)
            self.probe_q.put(it)
        self._regrid()
        self.save_queue_soon()
        msg = "已添加 %d 个文件" % len(new)
        if live and new:
            msg += "，已排在队尾，前面的转完会自动接着转"
            diag.trail("转换中追加 %d 个文件" % len(new))
        if skipped_dup:
            msg += "，跳过 %d 个重复" % skipped_dup
        if skipped_limit:
            msg += "，超过 %d 个上限的未添加" % MAX_FILES
        if new or skipped_dup or skipped_limit:
            self.toast(msg, color=C_WARN if skipped_limit else None)
        log.info(msg)

    def _make_row(self, it):
        row = FileRow(self.list, self, it)
        self.rows[it.id] = row

    def _regrid(self):
        self.empty_lbl.pack_forget()
        for it in self.items:
            self.rows[it.id].pack_forget()
        for i, it in enumerate(self.items, 1):
            row = self.rows[it.id]
            row.set_index(i)
            row.pack(fill="x", pady=4, padx=(0, 4))
            row.update_controls()
            row.set_selected(self.selected == it.id)
        if not self.items:
            self.empty_lbl.pack(pady=60)
        self._refresh_summary()

    def select_item(self, it):
        self.selected = it.id
        for i in self.items:
            self.rows[i.id].set_selected(i.id == it.id)
        self.panel._schedule_preview()

    def move_item(self, it, d):
        diag.trail("移动文件 %+d：%s" % (d, it.name))
        with self._mut():
            i = self.items.index(it)
            j = i + d
            if 0 <= j < len(self.items):
                self.items[i], self.items[j] = self.items[j], self.items[i]
        self._regrid()
        self.save_queue_soon()

    def remove_item(self, it):
        diag.trail("移除文件：%s" % it.name)
        with self._mut():
            if self.running and it.status == "转换中":
                self.toast("这个文件正在转换，请先点“取消”再移除", color=C_WARN)
                return
            self.items.remove(it)
            it.in_batch = False
        row = self.rows.pop(it.id, None)
        if row:
            row.destroy()
        if self.selected == it.id:
            self.selected = None
        self._regrid()
        self.save_queue_soon()
        if self.running:
            self._update_total()

    def clear_items(self):
        if self.running:
            return
        diag.trail("清空列表（%d 个）" % len(self.items))
        for it in self.items:
            row = self.rows.pop(it.id, None)
            if row:
                row.destroy()
        self.items = []
        self.selected = None
        self.total_bar.set(0)
        self._regrid()
        self.save_queue_soon()

    def _merge_toggled(self):
        diag.trail("合并开关：%s" % self.merge_var.get())
        for it in self.items:
            self.rows[it.id].update_controls()
        self._refresh_summary()

    # ------------------------------------------------------------ 队列保存与恢复
    def save_queue_soon(self):
        if self._queue_job:
            try:
                self.root.after_cancel(self._queue_job)
            except Exception:
                pass
        self._queue_job = self.root.after(800, self._save_queue)

    def _save_queue(self):
        self._queue_job = None
        try:
            keep = [{"path": i.path, "settings": i.settings} for i in self.items
                    if i.status in ("等待", "转换中", "已取消", "失败", "探测中")]
            p = os.path.join(self.data_dir, "queue.json")
            if not keep:
                if os.path.exists(p):
                    os.remove(p)
                return
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as e:
            log.warning("保存待转换队列失败：%s", e)

    def _restore_queue(self):
        p = os.path.join(self.data_dir, "queue.json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("读取待转换队列失败：%s", e)
            return
        n = 0
        for e in saved[:MAX_FILES]:
            try:
                path = e.get("path")
                if not path or not os.path.isfile(path):
                    continue
                it = runner.Item(path)
                it.settings = e.get("settings") or None
                self.items.append(it)
                self._make_row(it)
                self.probe_q.put(it)
                n += 1
            except Exception:
                continue
        if n:
            self._regrid()
            self.toast("已恢复上次没转完的 %d 个文件，点“开始转换”继续" % n, 9000)
            diag.trail("恢复队列 %d 个文件" % n)
            log.info("恢复队列：%d 个文件", n)

    # ------------------------------------------------------------ 探测
    def _probe_loop(self):
        while True:
            it = self.probe_q.get()
            try:
                it.probe = runner.probe_file(self.ffprobe, it.path)
                it.status = "等待"
                it.probe_error = None
            except runner.RunError as e:
                it.probe_error = str(e)
                it.status = "无法读取"
                it.message = str(e)
                log.warning("读取失败 %s：%s", it.name, e)
            except Exception as e:
                diag.record_error("探测 " + it.name, *sys.exc_info())
                it.probe_error = "读取失败：%s" % e
                it.status = "无法读取"
            self.q.put(("probed", it))

    def _get_ffver(self):
        try:
            if self.ffmpeg:
                r = subprocess.run([self.ffmpeg, "-version"], capture_output=True, timeout=20,
                                   creationflags=core.popen_flags())
                line = r.stdout.decode("utf-8", "replace").splitlines()[0]
                diag.set_env_extra(ffmpeg=line)
        except Exception as e:
            diag.set_env_extra(ffmpeg="读取失败：%s" % e)

    # ------------------------------------------------------------ 设置相关
    def current_item(self):
        for it in self.items:
            if it.id == self.selected:
                return it
        for it in self.items:
            if it.probe:
                return it
        return None

    def sv_ready(self):
        sv = self.cfg.get("sv") or {}
        return bool(sv.get("exe") and os.path.isfile(sv["exe"]) and sv.get("model") and os.path.isfile(sv["model"]))

    def settings_for(self, it, snap):
        if it.settings:
            return core.norm(it.settings)
        if it.snap:
            return core.norm(it.snap)
        return core.norm(snap)

    def preview_text(self, it, s):
        if it is None or it.probe is None:
            return "（添加文件后，这里会显示实际执行的转换命令。点击文件可切换预览对象。）"
        if not self.ffmpeg:
            return "（找不到转换引擎）"
        try:
            s = core.norm(s)
            t = s["target"]
            if t == "srt":
                tracks = core.srt_tracks(it.probe, s)
                plan = core.build_srt_cmd(self.ffmpeg, it.probe, tracks[0][1], "输出.srt")
                return plan.text()
            if t == "txt":
                plan = core.plan_asr_wav(self.ffmpeg, it.probe, s, "识别用.wav")
                sv = self.cfg.get("sv") or {}
                second = core.cmd_text([sv.get("exe") or "llama-funasr-sensevoice", "-m", sv.get("model") or "模型.gguf"] +
                                       (["--vad", sv["vad"]] if sv.get("vad") else []) + ["-a", "识别用.wav"])
                return "① " + plan.text() + "\n② " + second
            if self.merge_var.get() and len([i for i in self.items if i.probe]) >= 2:
                plan = core.plan_merge(self.ffmpeg, [i.probe for i in self.items if i.probe], s, "合并输出." + core.OUT_EXT[t])
            else:
                plan = core.plan_for(self.ffmpeg, it.probe, s, "输出." + core.OUT_EXT[t])
            txt = "［%s］\n%s" % (plan.mode, plan.text())
            if plan.notes:
                txt += "\n提示：" + "；".join(plan.notes)
            return txt
        except core.BuildError as e:
            return "⚠ " + str(e)
        except ValueError as e:
            return "⚠ " + str(e)
        except Exception as e:
            return "（预览失败：%s）" % e

    def _preview_global(self, s):
        return self.preview_text(self.current_item(), s)

    def edit_item_settings(self, it):
        if it.probe is None or (self.running and (self.mode != "batch" or self.run_merge or it.status == "转换中")):
            return
        diag.trail("打开单独设置：%s" % it.name)
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("单独设置 · " + ellipsize(it.name, 40))
        center_on(dlg, self.root, 460, 700)
        dlg.after(150, lambda: (dlg.lift(), dlg.focus_force()))
        ctk.CTkLabel(dlg, text="只对这个文件生效；其他文件仍使用右侧的统一设置。", font=F(12), text_color=C_MUTED,
                     anchor="w").pack(fill="x", padx=16, pady=(12, 0))
        sc = ctk.CTkScrollableFrame(dlg, fg_color=C_PANEL)
        sc.pack(fill="both", expand=True, padx=12, pady=8)
        base = dict(it.settings) if it.settings else dict(self.panel.get())
        base["preset"] = base.get("preset", "")
        panel = SettingsPanel(sc, self, base, lambda s: None, probe=it.probe, show_trim=True,
                              preview_fn=lambda s: self.preview_text(it, s))
        panel.adv_open = True
        panel._update_adv_button()
        panel.pack(fill="x", padx=8, pady=8)
        bar = ctk.CTkFrame(dlg, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(0, 14))

        def ok():
            it.settings = panel.get()
            diag.trail("保存单独设置：%s → %s" % (it.name, it.settings.get("target")))
            self.rows[it.id].refresh()
            dlg.destroy()
            self._refresh_summary()
            self.panel._schedule_preview()

        def reset():
            it.settings = None
            self.rows[it.id].refresh()
            dlg.destroy()
            self._refresh_summary()

        ctk.CTkButton(bar, text="确定", width=80, font=F(13), command=ok).pack(side="right")
        ctk.CTkButton(bar, text="取消", width=70, font=F(13), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=dlg.destroy).pack(side="right", padx=8)
        ctk.CTkButton(bar, text="恢复统一设置", width=100, font=F(13), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=reset).pack(side="left")
        try:
            dlg.after(300, dlg.grab_set)
        except Exception:
            pass

    def save_current_as_preset(self, panel):
        dlg = ctk.CTkInputDialog(text="给这组设置起个名字：", title="保存为我的预设")
        name = (dlg.get_input() or "").strip()
        if not name:
            return
        if name in core.BUILTIN_PRESETS:
            name += "（我的）"
        s = dict(panel.get())
        for k in ("trim_start", "trim_end", "preset", "force_encode"):
            s.pop(k, None)
        self.user_presets[name] = s
        diag.trail("保存预设：%s" % name)
        self.save_cfg_soon()
        self.panel.refresh_presets()
        panel.refresh_presets()
        panel.loading = True
        panel.w["preset"].set_value("u:" + name)
        panel.cur["preset"] = "u:" + name
        panel.loading = False
        self.toast("已保存预设：" + name)

    def delete_preset(self, panel, value):
        if not value or not value.startswith("u:"):
            self.toast("只能删除“★”开头的我的预设", color=C_WARN)
            return
        name = value[2:]
        if messagebox.askyesno("删除预设", "确定删除预设“%s”吗？" % name, parent=self.root):
            self.user_presets.pop(name, None)
            diag.trail("删除预设：%s" % name)
            self.save_cfg_soon()
            panel.refresh_presets()
            self.panel.refresh_presets()
            panel.w["preset"].set_value("")
            panel.cur["preset"] = ""

    # ------------------------------------------------------------ SenseVoice 设置
    def open_sv_dialog(self):
        diag.trail("打开设置")
        if self._sv_dlg is not None:
            try:
                self._sv_dlg.lift()
                return
            except Exception:
                self._sv_dlg = None
        dlg = ctk.CTkToplevel(self.root)
        self._sv_dlg = dlg
        dlg.title("设置 · SenseVoice 语音识别")
        center_on(dlg, self.root, 700, 380)
        dlg.after(150, lambda: (dlg.lift(), dlg.focus_force()))
        sv = self.cfg.get("sv") or {}
        ctk.CTkLabel(dlg, text="选好你已经部署的 SenseVoice 文件，“转换成”选“文字 · 语音识别 TXT”时就会用到。",
                     font=F(12), text_color=C_MUTED, anchor="w", wraplength=660, justify="left").pack(fill="x", padx=18, pady=(14, 8))
        vars_ = {}
        specs = [("exe", "程序（llama-funasr-sensevoice.exe）", [("程序", "*.exe"), ("所有文件", "*.*")]),
                 ("model", "模型文件（如 sensevoice-small-f16.gguf）", [("模型", "*.gguf"), ("所有文件", "*.*")]),
                 ("vad", "VAD 文件（fsmn-vad.gguf，长录音建议选）", [("VAD", "*.gguf"), ("所有文件", "*.*")])]
        status = ctk.CTkLabel(dlg, text="", anchor="w", justify="left", font=F(12), wraplength=660)
        self._sv_status = status

        def pick(key, ft):
            cur = vars_[key].get()
            p = filedialog.askopenfilename(parent=dlg, title="选择文件", filetypes=ft,
                                           initialdir=os.path.dirname(cur) if cur else None)
            if not p:
                return
            vars_[key].set(os.path.normpath(p))
            if key == "exe":
                autofill(False)

        def autofill(force):
            exe = vars_["exe"].get().strip()
            if not exe:
                status.configure(text="请先选择程序文件。", text_color=C_WARN)
                return
            m, v = core.find_sv_files(exe)
            got = []
            if m and (force or not vars_["model"].get()):
                vars_["model"].set(os.path.normpath(m))
                got.append("模型")
            if v and (force or not vars_["vad"].get()):
                vars_["vad"].set(os.path.normpath(v))
                got.append("VAD")
            if got:
                status.configure(text="已自动找到：" + "、".join(got) + "。", text_color=C_OK)
            elif force:
                status.configure(text="没有在程序旁边的 gguf 文件夹里找到模型，请手动选择。", text_color=C_WARN)

        for key, label, ft in specs:
            ctk.CTkLabel(dlg, text=label, anchor="w", font=F(12, True)).pack(fill="x", padx=18, pady=(8, 0))
            row = ctk.CTkFrame(dlg, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=(2, 0))
            v = tk.StringVar(value=sv.get(key, ""))
            vars_[key] = v
            ctk.CTkEntry(row, textvariable=v, height=30, font=F(12), fg_color=C_CARD, border_color=C_CARD_BORDER).pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="选择…", width=70, height=30, font=F(12), command=lambda k=key, f=ft: pick(k, f)).pack(side="left", padx=(8, 0))
        status.pack(fill="x", padx=18, pady=(12, 0))
        bar = ctk.CTkFrame(dlg, fg_color="transparent")
        bar.pack(fill="x", padx=18, pady=(12, 14), side="bottom")

        def current():
            return {k: vars_[k].get().strip() for k in ("exe", "model", "vad")}

        def test():
            c = current()
            if not c["exe"] or not os.path.isfile(c["exe"]):
                status.configure(text="程序文件不存在，请重新选择。", text_color=C_ERR)
                return
            status.configure(text="正在测试程序能否启动…", text_color=C_MUTED)
            threading.Thread(target=self._sv_test, args=(c,), daemon=True, name="SV测试").start()

        def save():
            c = current()
            problems = []
            for k, name in (("exe", "程序"), ("model", "模型")):
                if not c[k] or not os.path.isfile(c[k]):
                    problems.append(name)
            if c["vad"] and not os.path.isfile(c["vad"]):
                problems.append("VAD")
            self.cfg["sv"] = c
            self.save_cfg_soon()
            diag.trail("保存 SenseVoice 路径（程序%s 模型%s VAD%s）" % ("✓" if c["exe"] else "×", "✓" if c["model"] else "×", "✓" if c["vad"] else "×"))
            self.panel.refresh_sv()
            if problems:
                status.configure(text="已保存，但这些文件不存在：" + "、".join(problems) + "。", text_color=C_WARN)
            else:
                self.toast("SenseVoice 路径已保存")
                close()

        def close():
            self._sv_dlg = None
            self._sv_status = None
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", close)
        ctk.CTkButton(bar, text="自动查找模型", width=110, font=F(12), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=lambda: autofill(True)).pack(side="left")
        ctk.CTkButton(bar, text="测试程序", width=90, font=F(12), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=test).pack(side="left", padx=8)
        ctk.CTkButton(bar, text="保存", width=80, font=F(13, True), command=save).pack(side="right")
        ctk.CTkButton(bar, text="取消", width=70, font=F(13), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=close).pack(side="right", padx=8)

    def _sv_test(self, c):
        try:
            r = subprocess.run([c["exe"]], capture_output=True, timeout=20, stdin=subprocess.DEVNULL,
                               cwd=os.path.dirname(c["exe"]) or None, creationflags=core.popen_flags())
            code = r.returncode & 0xFFFFFFFF
            out = (r.stdout + r.stderr).decode("utf-8", "replace").strip().splitlines()
            if code == 0xC000001D:
                msg = ("✗ 程序无法在这台电脑上运行（处理器不支持它需要的指令）。请换用 funasr-llamacpp-windows-x64.zip（不带 avx2）。", C_ERR)
            elif code in (0xC0000135, 0xC0000139):
                msg = ("✗ 缺少运行库，请安装“Microsoft Visual C++ 运行库”。", C_ERR)
            else:
                msg = ("✓ 程序可以启动（不带参数运行会显示用法说明，属于正常）。" + (" 首行：" + out[0][:80] if out else ""), C_OK)
            log.info("SenseVoice 测试：退出码 %s", r.returncode)
        except subprocess.TimeoutExpired:
            msg = ("程序运行超过 20 秒没有结束（可能在等待输入），但它能启动。", C_WARN)
        except Exception as e:
            msg = ("✗ 无法启动程序：%s" % e, C_ERR)
        self.q.put(("sv_test", msg))

    # ------------------------------------------------------------ 运行
    def _set_running(self, on, mode=""):
        self.running = on
        self.mode = mode if on else ""
        for it in self.items:
            self.rows[it.id].update_controls()
        self.clear_btn.configure(state="disabled" if on else "normal")
        self.merge_chk.configure(state="disabled" if on else "normal")
        power.keep_awake(bool(on))
        if on:
            self.start_btn.grid_forget()
            self.trial_btn.grid_forget()
            self.sched_btn.grid_forget()
            self.cancel_btn.grid(row=0, column=4)
            self.cancel_btn.configure(state="normal", text="取消")
            if mode == "batch" and not self.run_merge:
                self.pause_after_var.set(False)
                self.pause_btn.configure(text="暂停", state="normal", fg_color=C_WARN, hover_color=C_WARN_HOVER)
                self.pause_btn.grid(row=0, column=3, padx=(0, 8))
                self.pause_after_chk.grid(row=0, column=2, padx=(0, 12))
        else:
            self.jump_item = None
            self.run_merge = False
            self.cancel_btn.grid_forget()
            self.pause_btn.grid_forget()
            self.pause_after_chk.grid_forget()
            self.sched_btn.grid(row=0, column=2, padx=(0, 8))
            self.trial_btn.grid(row=0, column=3, padx=(0, 8))
            self.start_btn.grid(row=0, column=4)
        self._refresh_summary()

    def _make_ctx(self):
        sv = dict(self.cfg.get("sv") or {})
        opts = {"simplified": bool(self.asr_vars["asr_simplified"].get()), "tidy": bool(self.asr_vars["asr_tidy"].get()),
                "split": bool(self.asr_vars["asr_split"].get())}
        return runner.Ctx(self.ffmpeg, self.ffprobe, lambda *a: self.q.put(a), self.tmp_dir,
                          low_priority=bool(self.low_var.get()), out_mode=self.cfg.get("out_mode", "folder"),
                          out_custom=self.cfg.get("out_custom", ""), sv=sv, data_dir=self.data_dir, asr_opts=opts)

    # ------------------------------------------------------------ 暂停 / 继续 / 先转
    def item_paused(self, it):
        c = self.ctx
        return bool(self.running and self.mode == "batch" and c is not None and c.paused and
                    it.status == "转换中" and it is not self.jump_item)

    def can_jump(self, it):
        c = self.ctx
        return bool(self.running and self.mode == "batch" and not self.run_merge and c is not None and c.paused and
                    self.jump_item is None and c.jump_thread is None and it.in_batch and it.status == "等待" and
                    it.probe is not None)

    def toggle_pause(self):
        c = self.ctx
        if not (self.running and self.mode == "batch" and c is not None):
            return
        if c.paused:
            diag.trail("点击 继续")
            c.resume()
        else:
            diag.trail("点击 暂停")
            c.pause()

    def pause_after_toggled(self):
        v = bool(self.pause_after_var.get())
        diag.trail("当前文件完成后暂停：%s" % ("开" if v else "关"))
        if self.ctx is not None:
            self.ctx.pause_after = v

    def run_first(self, it):
        diag.trail("点击 先转这个：%s" % it.name)
        snap = self.run_snap
        if not (self.ctx and runner.start_jump(self.ctx, it, lambda i: self.settings_for(i, snap))):
            self.toast("现在不能先转这个文件", color=C_WARN)

    def _apply_pause_ui(self, paused, reason=""):
        if not self.running or self.mode != "batch":
            return
        if paused:
            self.pause_btn.configure(text="继续", fg_color=C_ACCENT, hover_color=C_ACCENT_HOVER)
            if reason == "after":
                self.pause_after_var.set(False)
                self.toast("当前文件已完成，已暂停。点“继续”接着转下一个。", 8000, C_WARN)
        else:
            self.pause_btn.configure(text="暂停", fg_color=C_WARN, hover_color=C_WARN_HOVER)
        power.keep_awake(not paused)
        for it in self.items:
            row = self.rows.get(it.id)
            if row:
                row.refresh()
                row.update_controls()
        self._update_total()

    def _apply_lane(self, it, lane):
        self.jump_item = it if lane == "jump" else None
        if lane == "jump":
            self.pause_btn.configure(text="先转中…", state="disabled")
            self.toast("正在先转所选文件，转完会自动回到暂停的文件继续", 6000)
        else:
            self.pause_btn.configure(state="normal")
        for i in self.items:
            row = self.rows.get(i.id)
            if row:
                row.refresh()
                row.update_controls()

    def start(self, auto=False):
        """auto=True 表示定时开始：没有人在场，所有需要确认的地方都按默认处理，不弹窗。"""
        diag.trail("定时开始触发" if auto else "点击 开始转换")
        if self.running:
            return
        if not auto:
            self.cancel_sched(silent=True)

        def warn(title, msg):
            if auto:
                self.toast(msg, 10000, C_WARN)
                log.warning("定时开始被中止：%s", msg)
            else:
                messagebox.showwarning(title, msg, parent=self.root)

        if not (self.ffmpeg and self.ffprobe):
            if auto:
                log.warning("定时开始被中止：缺少 ffmpeg")
            else:
                messagebox.showerror("缺少转换引擎", "找不到 ffmpeg。请确认程序文件夹里的 ffmpeg 文件夹完整。", parent=self.root)
            return
        todo = [i for i in self.items if i.probe and i.status in ("等待", "失败", "已取消")]
        if not todo:
            done_items = [i for i in self.items if i.probe and i.status == "完成"]
            if (not auto) and done_items and messagebox.askyesno("重新转换", "列表里的文件都已转换完成。要全部再转换一次吗？", parent=self.root):
                for i in done_items:
                    i.status, i.pct, i.message = "等待", 0, ""
                    self.rows[i.id].refresh()
                todo = done_items
            if not todo:
                if not done_items:
                    self.toast("没有可转换的文件，请先添加", color=C_WARN)
                return
        snap = core.norm(self.panel.get())
        merge = bool(self.merge_var.get()) and len(todo) >= 2
        # 检查：语音识别路径
        need_sv = (not merge and any(self.settings_for(i, snap)["target"] == "txt" for i in todo)) or (merge and snap["target"] == "txt")
        if need_sv and not self.sv_ready():
            if auto:
                warn("", "还没有设置 SenseVoice 路径，定时转换没有开始。")
            elif messagebox.askyesno("还没设置 SenseVoice", "语音转文字需要先选择 SenseVoice 的程序和模型文件。现在去设置吗？", parent=self.root):
                self.open_sv_dialog()
            return
        if merge and snap["target"] in ("txt", "srt", "gif"):
            warn("不能合并", "合并只支持常规视频或音频格式。请取消合并，或重新选择“转换成”。")
            return
        # 剪切时间等设置的合法性（提前发现）
        try:
            for i in todo:
                s = self.settings_for(i, snap)
                core.parse_time(s["trim_start"])
                core.parse_time(s["trim_end"])
        except ValueError as e:
            warn("设置有误", str(e))
            return
        # 磁盘空间
        if not auto:
            first = todo[0]
            d = core.out_dir_for(first.path, self.cfg.get("out_mode", "folder"), self.cfg.get("out_custom", ""))
            total_in = sum(i.probe.size for i in todo)
            free, ok = runner.check_space(d, 1024 ** 3)
            if free is not None and (free < 1024 ** 3 or free < total_in):
                if not messagebox.askyesno("磁盘空间可能不够", "保存位置所在磁盘剩余 %s，待转换文件共 %s。\n空间不足会导致转换失败，仍要继续吗？" %
                                           (core.human_size(free), core.human_size(total_in)), parent=self.root):
                    return
        self.run_merge = merge
        self.run_snap = snap
        self.ctx = self._make_ctx()
        for i in self.items:
            i.in_batch = False
            i.snap = None            # 每次点“开始”，都以当前界面设置为准
        for i in todo:
            i.in_batch = True
            i.status, i.pct, i.message, i.elapsed, i.out_paths, i.eta, i.approx = "等待", 0, "", None, [], None, False
            self.rows[i.id].refresh()
        self.last_outs = []
        self.total_bar.set(0)
        self._set_running(True, "batch")
        log.info("开始批量转换：%d 个文件，合并=%s，目标=%s，定时=%s", len(todo), merge, snap["target"], auto)
        threading.Thread(target=runner.run_batch, name="转换线程", daemon=True,
                         args=(self.ctx, self.items, lambda it: self.settings_for(it, snap), merge, snap)).start()

    def cancel(self):
        diag.trail("点击 取消")
        if self.ctx:
            self.ctx.request_cancel()
        self.cancel_btn.configure(state="disabled", text="正在取消…")
        self.pause_btn.configure(state="disabled")

    def trial(self):
        diag.trail("点击 试转前30秒")
        if self.running:
            return
        it = self.current_item()
        if it is None or it.probe is None:
            self.toast("请先添加文件（点击文件可选择试转哪一个）", color=C_WARN)
            return
        if self.merge_var.get():
            self.toast("合并模式下不能试转，请先取消合并", color=C_WARN)
            return
        snap = core.norm(self.panel.get())
        s = self.settings_for(it, snap)
        if s["target"] == "srt":
            self.toast("字幕提取很快，直接点“开始转换”即可", color=C_WARN)
            return
        if s["target"] == "txt" and not self.sv_ready():
            self.open_sv_dialog()
            return
        try:
            core.parse_time(s["trim_start"])
            core.parse_time(s["trim_end"])
        except ValueError as e:
            messagebox.showwarning("设置有误", str(e), parent=self.root)
            return
        self.run_merge = False
        self.ctx = self._make_ctx()
        it.prev_status = it.status
        it.status, it.pct, it.message = "转换中", 0, "试转前 30 秒…"
        self.rows[it.id].refresh()
        self._set_running(True, "trial")
        ctx = self.ctx

        def work():
            try:
                res = runner.run_trial(ctx, it, s, 30)
                self.q.put(("trial_done", it, res, None))
            except runner.Cancelled:
                self.q.put(("trial_done", it, None, "已取消"))
            except (runner.RunError, core.BuildError) as e:
                self.q.put(("trial_done", it, None, str(e)))
            except Exception as e:
                diag.record_error("试转", *sys.exc_info())
                self.q.put(("trial_done", it, None, "程序内部错误：%s，请复制诊断报告发给我。" % e))
        threading.Thread(target=work, name="试转线程", daemon=True).start()

    # ------------------------------------------------------------ 定时开始
    def _sched_text(self):
        d = self.sched_at
        today = datetime.date.today()
        day = "" if d.date() == today else ("明天 " if d.date() == today + datetime.timedelta(days=1) else d.strftime("%m-%d "))
        return day + d.strftime("%H:%M")

    def sched_clicked(self):
        if self.sched_at is not None:
            self.cancel_sched()
        else:
            self.open_sched_dialog()

    def open_sched_dialog(self):
        diag.trail("点击 定时开始")
        if self.running:
            return
        todo = [i for i in self.items if i.probe and i.status in ("等待", "失败", "已取消")]
        if not todo:
            self.toast("请先添加要转换的文件", color=C_WARN)
            return
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("定时开始")
        center_on(dlg, self.root, 420, 290)
        dlg.after(150, lambda: (dlg.lift(), dlg.focus_force()))
        ctk.CTkLabel(dlg, text="几点自动开始转换？", font=F(15, True), anchor="w").pack(fill="x", padx=22, pady=(20, 8))
        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(fill="x", padx=22)
        base = datetime.datetime.now() + datetime.timedelta(hours=1)
        hv = tk.StringVar(value="%02d" % base.hour)
        mv = tk.StringVar(value="%02d" % (base.minute // 5 * 5))
        ctk.CTkOptionMenu(row, values=["%02d" % h for h in range(24)], variable=hv, width=76, font=F(14)).pack(side="left")
        ctk.CTkLabel(row, text=" 时 ", font=F(14)).pack(side="left")
        ctk.CTkOptionMenu(row, values=["%02d" % m for m in range(0, 60, 5)], variable=mv, width=76, font=F(14)).pack(side="left")
        ctk.CTkLabel(row, text=" 分", font=F(14)).pack(side="left")
        ctk.CTkLabel(dlg, text="到点后会自动开始。请保持电脑开机、本软件开着（等待期间软件会阻止电脑自动睡眠）。"
                               "如果勾选了“全部完成后自动关机”，转完会自动关机。",
                     font=F(12), text_color=C_MUTED, anchor="w", justify="left", wraplength=370).pack(fill="x", padx=22, pady=(14, 0))
        bar = ctk.CTkFrame(dlg, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=22, pady=(0, 18))

        def ok():
            now = datetime.datetime.now()
            t = now.replace(hour=int(hv.get()), minute=int(mv.get()), second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            self.sched_at = t
            self.sched_btn.configure(text="取消定时")
            power.keep_awake(True)
            diag.trail("设定定时开始：%s" % self._sched_text())
            log.info("设定定时开始：%s", t.strftime("%Y-%m-%d %H:%M"))
            dlg.destroy()
            self._refresh_summary()
            self.toast("已定时：%s 自动开始" % self._sched_text(), 6000)
            self._sched_tick()

        ctk.CTkButton(bar, text="确定", width=80, font=F(13, True), command=ok).pack(side="right")
        ctk.CTkButton(bar, text="取消", width=70, font=F(13), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=dlg.destroy).pack(side="right", padx=8)
        try:
            dlg.after(300, dlg.grab_set)
        except Exception:
            pass

    def _sched_tick(self):
        self._sched_job = None
        if self.sched_at is None or self.running:
            return
        if datetime.datetime.now() >= self.sched_at:
            self.sched_at = None
            self.sched_btn.configure(text="定时开始")
            power.keep_awake(False)
            self.start(auto=True)
            return
        self._sched_job = self.root.after(1000, self._sched_tick)

    def cancel_sched(self, silent=False):
        if self.sched_at is None:
            return
        diag.trail("取消定时开始")
        self.sched_at = None
        if self._sched_job:
            try:
                self.root.after_cancel(self._sched_job)
            except Exception:
                pass
            self._sched_job = None
        self.sched_btn.configure(text="定时开始")
        if not self.running:
            power.keep_awake(False)
        self._refresh_summary()
        if not silent:
            self.toast("已取消定时")

    # ------------------------------------------------------------ 事件处理
    def _pump(self):
        try:
            for _ in range(300):
                try:
                    ev = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(ev)
                except Exception:
                    diag.record_error("事件处理 %s" % (ev[0] if ev else "?"), *sys.exc_info())
        finally:
            try:
                self.root.after(100, self._pump)
            except Exception:
                pass

    def _handle(self, ev):
        k = ev[0]
        if k == "probed":
            it = ev[1]
            row = self.rows.get(it.id)
            if row:
                row.refresh()
                row.update_controls()
            self.panel._schedule_preview()
            self._refresh_summary()
            self.save_queue_soon()
        elif k == "progress":
            _, it, pct, text = ev
            it.pct, it.message = pct, text
            row = self.rows.get(it.id)
            if row and it.status == "转换中" and not self.item_paused(it):
                row.set_progress(pct, it.approx)
                row.msg.configure(text=text, text_color=C_MUTED)
                row.msg.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(6, 6), pady=(0, 8))
            self._update_total()
        elif k == "status":
            _, it, st, msg = ev
            it.status = st
            if msg:
                it.message = msg
            row = self.rows.get(it.id)
            if row:
                row.refresh()
                row.update_controls()
            self._update_total()
            self._refresh_summary()
            self.save_queue_soon()
        elif k == "pause_state":
            self._apply_pause_ui(bool(ev[2]), ev[3])
        elif k == "lane":
            self._apply_lane(ev[1], ev[2])
        elif k == "batch_done":
            self._batch_done(ev[2])
        elif k == "trial_done":
            self._trial_done(ev[1], ev[2], ev[3])
        elif k == "sv_test":
            if self._sv_status is not None:
                try:
                    self._sv_status.configure(text=ev[1][0], text_color=ev[1][1])
                except Exception:
                    pass
        elif k == "crash":
            messagebox.showerror("程序出错了", "程序遇到了意外错误。\n诊断报告已自动保存到：\n%s\n\n请把这个文件的内容（或点右上角橙色“复制诊断报告”）发给我。" % ev[1], parent=self.root)

    def _batch_list(self):
        return [i for i in self.items if i.in_batch and i.status != "无法读取"]

    def _update_total(self):
        if not (self.running and self.mode == "batch"):
            return
        items = self._batch_list()
        if not items:
            return
        n = len(items)
        done = sum(1 for i in items if i.status in ("完成", "失败", "已取消"))
        running = [i for i in items if i.status == "转换中"]
        if self.run_merge and n >= 2:
            val = max((i.pct or 0) for i in running) / 100.0 if running else (1.0 if done == n else 0.0)
        else:
            wsum = psum = 0.0
            for i in items:
                w = float(i.probe.duration) if (i.probe and i.probe.duration) else 60.0
                p = 1.0 if i.status in ("完成", "失败", "已取消") else ((i.pct or 0) / 100.0 if i.status == "转换中" else 0.0)
                wsum += w
                psum += w * p
            val = psum / wsum if wsum else 0.0
        val = max(0.0, min(1.0, val))
        self.total_bar.set(val)
        self._total_pct = val * 100.0
        self._total_eta = None
        if self.ctx is not None and not self.run_merge:
            snap = self.run_snap
            try:
                self._total_eta = runner.estimate_remaining(self.ctx, self.items, lambda i: self.settings_for(i, snap))
            except Exception:
                self._total_eta = None
        self._refresh_summary()

    def _refresh_summary(self):
        n = len(self.items)
        if self.running and self.mode == "batch":
            its = self._batch_list()
            done = sum(1 for i in its if i.status in ("完成", "失败", "已取消"))
            pct = getattr(self, "_total_pct", 0.0)
            parts = []
            if self.ctx is not None and self.ctx.paused:
                parts.append("已暂停")
            parts.append("总进度 %d%%" % int(pct))
            parts.append("第 %d/%d 个" % (min(done + 1, len(its)), len(its)))
            eta = getattr(self, "_total_eta", None)
            if eta is not None and not (self.ctx is not None and self.ctx.paused):
                parts.append("剩余约 %s（估算）" % core.fmt_hms(eta))
            self.sum_lbl.configure(text=" · ".join(parts))
        elif self.running:
            self.sum_lbl.configure(text="试转中…")
        elif self.sched_at is not None:
            self.sum_lbl.configure(text="⏰ 将在 %s 自动开始" % self._sched_text())
        else:
            ok = sum(1 for i in self.items if i.status == "完成")
            bad = sum(1 for i in self.items if i.status in ("失败", "无法读取"))
            txt = "共 %d 个文件" % n if n else "还没有文件"
            if ok:
                txt += " · 完成 %d" % ok
            if bad:
                txt += " · 失败 %d" % bad
            self.sum_lbl.configure(text=txt)

    def _batch_done(self, stats):
        log.info("批量转换结束：%s", {k: v for k, v in stats.items() if k != "outs"})
        self._set_running(False)
        self._total_eta = None
        self.last_outs = stats.get("outs", [])
        ok, bad, canc = stats.get("ok", 0), stats.get("fail", 0), stats.get("cancel", 0)
        if not canc and ok and not bad:
            self.total_bar.set(1.0)
        leftover = [i for i in self.items if i.in_batch and i.status == "等待" and i.probe is not None]
        for it in self.items:
            it.in_batch = False
            if it.id in self.rows:
                self.rows[it.id].refresh()
                self.rows[it.id].update_controls()
        self._refresh_summary()
        self.save_queue_soon()
        try:
            self.root.bell()
        except Exception:
            pass
        if leftover:
            self.toast("刚加进来的 %d 个文件还没转，请再点“开始转换”。" % len(leftover), 9000, C_WARN)
        if canc:
            self.toast("已取消。未完成的文件已恢复为“已取消”，可再次点“开始转换”。", 6000, C_WARN)
            return
        if ok and self.open_var.get() and self.last_outs:
            open_path(self.last_outs[-1], select=True)
        self._show_done(ok, bad)
        if self.shut_var.get():
            self._start_shutdown()

    def _show_done(self, ok, bad):
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("转换完成")
        center_on(dlg, self.root, 380, 190)
        dlg.after(150, lambda: (dlg.lift(), dlg.focus_force()))
        msg = "成功 %d 个" % ok + ("，失败 %d 个" % bad if bad else "")
        ctk.CTkLabel(dlg, text=msg, font=F(18, True), text_color=C_ERR if bad and not ok else (C_WARN if bad else C_OK)).pack(pady=(26, 4))
        ctk.CTkLabel(dlg, text="失败的文件会在列表里显示原因" if bad else "已保存，可在文件行点“打开”查看", font=F(12), text_color=C_MUTED).pack()
        bar = ctk.CTkFrame(dlg, fg_color="transparent")
        bar.pack(pady=18)
        if self.last_outs:
            ctk.CTkButton(bar, text="打开文件夹", width=100, font=F(13),
                          command=lambda: (open_path(self.last_outs[-1], select=True), dlg.destroy())).pack(side="left", padx=6)
        ctk.CTkButton(bar, text="关闭", width=80, font=F(13), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=dlg.destroy).pack(side="left", padx=6)

    def _start_shutdown(self):
        diag.trail("触发 完成后自动关机")
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("即将关机")
        center_on(dlg, self.root, 400, 190)
        dlg.after(150, lambda: (dlg.lift(), dlg.focus_force(), dlg.attributes("-topmost", True)))
        lbl = ctk.CTkLabel(dlg, text="", font=F(15, True))
        lbl.pack(pady=(32, 10))
        state = {"left": 60, "job": None, "live": True}
        if os.name == "nt":
            try:
                subprocess.run(["shutdown", "/s", "/t", "60", "/c", "格式转换已完成，电脑将在 60 秒后关机"],
                               creationflags=core.popen_flags(), timeout=10)
            except Exception as e:
                log.warning("设置关机失败：%s", e)

        def tick():
            if not state["live"]:
                return
            lbl.configure(text="转换已完成，电脑将在 %d 秒后关机" % state["left"])
            if state["left"] <= 0:
                return
            state["left"] -= 1
            state["job"] = dlg.after(1000, tick)

        def abort():
            state["live"] = False
            if os.name == "nt":
                try:
                    subprocess.run(["shutdown", "/a"], creationflags=core.popen_flags(), timeout=10)
                except Exception:
                    pass
            diag.trail("取消关机")
            self.shut_var.set(False)
            dlg.destroy()
            self.toast("已取消关机")

        ctk.CTkButton(dlg, text="取消关机", width=120, height=36, font=F(14, True), fg_color=C_ERR, command=abort).pack(pady=14)
        dlg.protocol("WM_DELETE_WINDOW", abort)
        tick()

    def _trial_done(self, it, res, err):
        self._set_running(False)
        it.status = getattr(it, "prev_status", "等待")
        it.message = ""
        row = self.rows.get(it.id)
        if row:
            row.refresh()
        if err:
            if err == "已取消":
                self.toast("已取消试转", color=C_WARN)
            else:
                messagebox.showwarning("试转失败", err, parent=self.root)
            return
        out, el, est = res
        diag.trail("试转完成 用时%.1fs 预计完整大小%s" % (el, core.human_size(est) if est else "?"))
        txt = "试转用时 %s。" % core.fmt_hms(el)
        if est:
            txt += "\n这个文件完整转换后，大小预计约 %s。" % core.human_size(est)
        if it.trial_text is not None:
            TextDialog(self.root, "试转结果（前 30 秒的识别文字）", txt, it.trial_text, 640, 460)
            return
        txt += "\n\n已用默认播放器打开试转文件，听/看一下效果。满意就点“开始转换”。"
        open_file(out)
        messagebox.showinfo("试转完成", txt, parent=self.root)

    def open_result(self, it):
        if it.out_paths:
            open_path(it.out_paths[0], select=True)

    # ------------------------------------------------------------ 诊断
    def copy_diag(self):
        diag.trail("点击 复制诊断报告")
        text = diag.build_report()
        path = diag.save_report(text)
        if diag.copy_text(self.root, text):
            self.toast("✓ 已复制诊断报告，请粘贴发给我", 6000)
        else:
            TextDialog(self.root, "诊断报告",
                       "无法自动复制到剪贴板。报告已保存到：%s\n请在下面的框里按 Ctrl+A 全选、Ctrl+C 复制，然后粘贴发给我。" % path,
                       text, 760, 560)

    def _state_text(self):
        return "运行中=%s（%s）；文件数=%d；合并=%s；已选中=%s；SenseVoice已设置=%s；拖拽可用=%s；转换引擎=%s" % (
            self.running, self.mode or "-", len(self.items), self.merge_var.get(), self.selected,
            self.sv_ready(), getattr(self.root, "dnd_ok", False), "正常" if (self.ffmpeg and self.ffprobe) else "缺失")

    def _config_text(self):
        c = dict(self.cfg)
        sv = c.get("sv") or {}
        c["sv"] = {k: (os.path.basename(v) + (" [存在]" if os.path.isfile(v) else " [不存在]")) if v else "" for k, v in sv.items()}
        c["user_presets"] = "%d 个" % len(self.user_presets)
        return json.dumps(c, ensure_ascii=False, indent=1)

    def _run_text(self):
        c = self.ctx
        lines = ["暂停=%s；完成后暂停=%s；先转中=%s；定时=%s；防睡眠=%s；排队中的追加文件=%d" % (
            bool(c and c.paused), bool(c and c.pause_after), self.jump_item.name if self.jump_item else "-",
            self._sched_text() if self.sched_at else "-", power.is_on(),
            sum(1 for i in self.items if i.snap and i.status in ("等待", "探测中")))]
        if c:
            lines.append("本轮已测得速度（处理 1 秒内容要几秒）：%s" % {k: round(v, 3) for k, v in c.rates.items()})
        try:
            lines.append("已记住的识别速度：%.3f" % runner.load_rtf(c or runner.Ctx(None, None, None, self.tmp_dir, data_dir=self.data_dir)))
        except Exception:
            pass
        try:
            rd = os.path.join(self.data_dir, "resume")
            lines.append("未完成的识别续转记录：%d 个" % (len(os.listdir(rd)) if os.path.isdir(rd) else 0))
        except Exception:
            pass
        lines.append("最近一次识别输出的格式特征（不含内容）：%s" % (runner.LAST_ASR_STATS or "（还没有）"))
        lines.append("文字整理选项：%s" % {k: bool(v.get()) for k, v in getattr(self, "asr_vars", {}).items()})
        return "\n".join(lines)

    def _items_text(self):
        if not self.items:
            return "（空）"
        return "\n".join("%d. %s" % (i, it.brief()) for i, it in enumerate(self.items[:60], 1))

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("正在转换", "还有转换正在进行，确定要退出吗？\n语音转文字已识别完的段落会保留，下次可以接着转；其他格式需要重新转。", parent=self.root):
                return
            if self.ctx:
                self.ctx.request_cancel()
            time.sleep(0.4)
        try:
            self._save_cfg()
            self._save_queue()
            power.keep_awake(False)
        except Exception:
            pass
        self._clean_tmp()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run(base_dir):
    app = App(base_dir)
    app.run()
