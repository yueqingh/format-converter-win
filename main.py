# -*- coding: utf-8 -*-
import os
import sys

import core
import diag

BASE = core.app_base_dir()


def _selftest():
    import selftest
    rep = selftest.run_selftest()
    text = rep.text()
    d = diag.data_dir() or BASE
    try:
        with open(os.path.join(d, "selftest_result.txt"), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    print(text)
    return 1 if rep.failed else 0


def main():
    diag.init(BASE)
    if "--selftest" in sys.argv:
        return _selftest()
    try:
        import ui
        ui.run(BASE)
    except Exception:
        et, ev, tb = sys.exc_info()
        diag.record_error("启动", et, ev, tb)
        path = diag.save_report(diag.build_report("启动失败"))
        try:
            import tkinter
            from tkinter import messagebox
            r = tkinter.Tk()
            r.withdraw()
            messagebox.showerror("格式转换 启动失败", "程序没能启动。\n诊断报告已保存到：\n%s\n\n请把这个文件的内容发给我。" % path)
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
