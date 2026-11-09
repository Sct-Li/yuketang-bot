# -*- coding: utf-8 -*-
"""GUI 冒烟测试：拉起 mock，跑一遍「读取课程 -> 选择 -> 刷课」，检查表格与完成情况。

    python tests/test_gui_smoke.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gui  # noqa: E402

PY = sys.executable
CFG = ROOT / "config.json"
HOST = "http://127.0.0.1:8901"


def wait_until(app, pred, timeout=60.0, label=""):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if pred():
            return True
        time.sleep(0.05)
    print(f"timeout waiting: {label}")
    return False


def main() -> int:
    srv = subprocess.Popen([PY, str(ROOT / "tests" / "mock_server.py"), "8901"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backup = CFG.read_text(encoding="utf-8") if CFG.exists() else None
    try:
        time.sleep(1.5)
        cfg = {"cookie_string": "csrftoken=x; sessionid=y; university_id=2803", "host": HOST,
               "step": 20, "batch": 50, "realtime": False, "gap": "1,2"}
        CFG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        app = gui.App()
        app.var_realtime.set(False)
        app.var_step.set(20)

        app.on_load()
        ok = wait_until(app, lambda: len(app.tree.get_children()) > 0, 40, "courses loaded")
        if not ok:
            app.destroy()
            return 1
        print("课程列表：", [app.rows[i]["name"] for i in app.tree.get_children()])

        ok = wait_until(app, lambda: all(app.rows[i]["state"] != "待扫描"
                                         for i in app.tree.get_children()), 150, "scan finished")
        cid = app.tree.get_children()[0]
        row = app.rows[cid]
        print("扫描结果：", {k: row[k] for k in ("chapters", "leaves", "videos", "done", "kind", "state")})
        if not ok or int(row["leaves"] or 0) != 3 or int(row["videos"] or 0) != 2:
            print("FAIL: 扫描数字不对")
            app.destroy()
            return 1

        app.tree.selection_set(cid)
        wait_until(app, lambda: not app.busy, 10, "idle before run")
        app.on_start()
        ok = wait_until(app, lambda: not app.busy, 90, "run finished")
        text = app.txt.get("1.0", "end")
        app.destroy()

        print("日志尾部：", " | ".join(text.strip().splitlines()[-3:]))
        passed = ok and ("'done': 2" in text)
        print("RESULT:", "PASS" if passed else "FAIL")
        return 0 if passed else 1
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()
        if backup is not None:
            CFG.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
