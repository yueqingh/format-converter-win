# -*- coding: utf-8 -*-
"""识别文字整理：去掉识别标记、繁体转简体、中文字之间多余空格、按句分行。纯函数，不依赖界面。"""
import re

try:
    from zh_t2s import T2S
except Exception:   # 字表缺失时只是不转换，不影响其他功能
    T2S = {}

_RE_TAG = re.compile(r"<\|[^|<>]{0,40}\|>")
_CJK = "㐀-鿿＀-￯　-〿"
_RE_CJK_SPACE = re.compile("(?<=[%s])[ \t　]+(?=[%s])" % (_CJK, _CJK))
_RE_SENT_CN = re.compile(r"([。！？…]+[”’」』）)\"']*)")
_RE_SENT_EN = re.compile(r"([!?]+)(?=\s|$)")
_RE_TS = re.compile(r"^\s*[\[\(]?\d{1,2}:\d{2}|-->|^\s*[\[\(]\d+(\.\d+)?\s*[-~,]")


def to_simplified(text):
    if not T2S or not text:
        return text
    return "".join(T2S.get(c, c) for c in text)


def analyze_raw(raw):
    """只统计格式特征，不含任何识别内容，可放进诊断报告。"""
    try:
        lines = [l for l in raw.splitlines() if l.strip()]
        return {
            "字符数": len(raw),
            "非空行数": len(lines),
            "识别标记个数": len(_RE_TAG.findall(raw)),
            "疑似时间戳行数": sum(1 for l in lines if _RE_TS.search(l)),
            "含繁体字": any(c in T2S for c in raw) if T2S else False,
            "最长一行字符数": max((len(l) for l in lines), default=0),
        }
    except Exception:
        return {}


def clean_text(raw, simplified=True, tidy=True, split=True):
    """整理识别文字。tidy：去标记和多余空格；split：按句分行；simplified：转简体。"""
    t = raw.replace("\r\n", "\n").replace("\r", "\n")
    if tidy:
        t = _RE_TAG.sub("", t)
        t = _RE_CJK_SPACE.sub("", t)
    if simplified:
        t = to_simplified(t)
    if tidy and split:
        t = _RE_SENT_CN.sub(r"\1\n", t)
        t = _RE_SENT_EN.sub(r"\1\n", t)
    if tidy:
        lines = [l.strip() for l in t.split("\n")]
        t = "\n".join(l for l in lines if l)
    return t.strip()
