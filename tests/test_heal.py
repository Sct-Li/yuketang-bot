# -*- coding: utf-8 -*-
"""验证 401 自愈：GUI 扫描中遇到 AuthError 会重抓 cookie 并重试同一门课。

用 mock 服务 + 打桩的 capture.capture / pl.scan_course，不碰真实账号。

    python tests/test_heal.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import capture  # noqa: E402
import gui  # noqa: E402
import player as pl  # noqa: E402
from ykt import AuthError  # noqa: E402

PY = sys.executable
CFG = ROOT / "config.json"
HOST = "http://127.0.0.1:8901"
NEW_JAR = {"csrftoken": "fresh", "sessionid": "fresh", "university_id": "2803"}


def pump(app, seconds=30, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        if until and until():
            return True
        time.sleep(0.05)
    return bool(until and until())


def main() -> int:
    srv = subprocess.Popen([PY, str(ROOT / "tests" / "mock_server.py"), "8901"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backup = CFG.read_text(encoding="utf-8") if CFG.exists() else None
    try:
        time.sleep(1.5)
        CFG.write_text(json.dumps({"cookie_string": "csrftoken=old; sessionid=old; university_id=2803",
                                   "host": HOST}, ensure_ascii=False), encoding="utf-8")

        state = {"calls": 0, "heals": 0}

        def flaky_scan(client, course, **kw):
            state["calls"] += 1
            if state["calls"] == 1:                     # 第一门课就 401
                raise AuthError("401 GET /mooc-api/... -> expired")
            return {"classroom_id": str(course["classroom_id"]), "name": course["name"],
                    "chapters": 1, "leaves": 2, "videos": 1, "slides": 0, "others": 1,
                    "done": 0, "duration": 60.0, "kind": "mooc", "error": ""}

        def fake_capture(**kw):
            state["heals"] += 1
            return dict(NEW_JAR)

        app = gui.App(auto_load=False)
        with mock.patch.object(capture, "capture", side_effect=fake_capture), \
                mock.patch.object(capture, "save_cookies", lambda jar, to_config=True: ROOT / "cookie.txt"), \
                mock.patch.object(pl, "scan_course", side_effect=flaky_scan):
            app.on_load()
            ok = pump(app, 40, lambda: bool(app.tree.get_children()) and
                      all(app.rows[i]["state"] != "待扫描" for i in app.tree.get_children()))
            text = app.txt.get("1.0", "end")
            rows = {i: app.rows[i]["state"] for i in app.tree.get_children()}

        app.destroy()
        print("heal called:", state["heals"], "scan calls:", state["calls"])
        print("rows:", rows)
        print("log tail:", " | ".join(text.strip().splitlines()[-3:]))
        passed = (ok and state["heals"] >= 1 and state["calls"] >= 2
                  and all(v not in ("待扫描",) for v in rows.values()))
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
