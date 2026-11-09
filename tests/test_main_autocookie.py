# -*- coding: utf-8 -*-
"""验证 main.py 的自动捕捉接线：capture 产出 cookie -> 写 cookie.txt/config -> 继续跑流程。

真实浏览器/DPAPI 部分不在这里测（见 test_capture.py），这里把 capture.capture 换成
假返回值，检查 main 是否真的会去调、调完能接着识别课程。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import capture  # noqa: E402
import main  # noqa: E402

FAKE = {"sessionid": "fake-session", "csrftoken": "fake-csrf", "university_id": "2803"}
HOST = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8901"

calls = {"capture": 0}


def fake_capture(**kw):
    calls["capture"] += 1
    print("  [mock] capture.capture called with", kw)
    return dict(FAKE)


with mock.patch.object(capture, "capture", side_effect=fake_capture), \
        mock.patch.object(capture, "verify", return_value=(True, "测试同学")):
    rc = main.main(["--host", HOST, "--cookies", "auto", "--list"])

print("rc =", rc, "capture called", calls["capture"], "times")
cookie_txt = ROOT / "cookie.txt"
print("cookie.txt:", cookie_txt.read_text(encoding="utf-8").strip() if cookie_txt.exists() else "MISSING")
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
print("config.cookie_string:", cfg.get("cookie_string", "")[:60])
ok = rc == 0 and calls["capture"] == 1 and cookie_txt.exists() and "fake-session" in cfg.get("cookie_string", "")
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
