# -*- coding: utf-8 -*-
"""真实抓包：用带调试端口的浏览器打开雨课堂页面，记录它自己发的 XHR。

用途：图谱/学习空间的接口字段不公开，与其猜，不如让官方前端自己发一遍请求，
我们把 method / url / postData / response 状态全记下来。

    python sniff.py --url "https://changjiang.yuketang.cn/v2/web/studentLog/<classroom_id>" --seconds 25
    python sniff.py --url "https://changjiang.yuketang.cn/ai-workspace/lms-graph/<classroom_id>/video/<video_id>?is_chapter=1&node_id=<node_id>" --seconds 30

依赖：capture 里的 WS 客户端 + 已经登录过的独立 profile（%LOCALAPPDATA%\\ykt-capture-profile）。
结果：dump/sniff/<时间戳>.json（全量）+ 终端打印过滤后的接口清单。
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from paths import app_dir as _app_dir
ROOT = _app_dir()
sys.path.insert(0, str(ROOT))
import capture  # noqa: E402

LOG = logging.getLogger("sniff")
OUT = ROOT / "dump" / "sniff"

INTEREST = ("/api/", "/mooc-api/", "train_platform", "/video-log/", "/c27/", "knowledge_graph", "lms")


def http_json(url: str, method: str = "GET") -> Any:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ensure_browser(port: int = 9222) -> int:
    saved = capture.load_port()
    if saved and capture.debug_port_alive(saved):
        return saved
    if capture.debug_port_alive(port):
        return port
    found = capture.find_browser_exe()
    if not found:
        raise SystemExit("没找到 Edge/Chrome")
    name, exe = found
    capture.kill_stale_capture_browsers(capture.DEDICATED_PROFILE)
    port = capture.pick_port(port)
    capture.launch_debug_browser(exe, port, capture.DEDICATED_PROFILE, f"{capture.HOST}/v2/web/index")
    if not capture.wait_for_port(port, None, 40):
        raise SystemExit("调试端口没起来")
    capture.save_port(port)
    LOG.info("浏览器就绪：127.0.0.1:%d", port)
    return port


def open_page(port: int, url: str) -> str:
    """新建一个标签页，返回它的 webSocketDebuggerUrl。"""
    try:
        target = http_json(f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}", "PUT")
    except Exception:  # 老版本要 GET
        target = http_json(f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}", "GET")
    ws = target.get("webSocketDebuggerUrl")
    if not ws:
        raise SystemExit(f"新建标签页失败：{target}")
    return ws


def sniff(port: int, url: str, seconds: float = 25.0, bodies: bool = False) -> List[Dict[str, Any]]:
    """打开 url，记录 maxPostData 与（可选）响应 body。"""
    ws_url = open_page(port, "about:blank")
    sock = capture.ws_connect(ws_url)
    records: List[Dict[str, Any]] = []
    pending: Dict[str, Dict[str, Any]] = {}
    mid = 0

    def call(method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        nonlocal mid
        mid += 1
        return capture.cdp_call(sock, mid, method, params or {})

    try:
        call("Network.enable", {"maxPostDataSize": 8192})
        call("Page.enable")
        call("Page.navigate", {"url": url})
        deadline = time.time() + seconds
        sock.settimeout(1.0)
        while time.time() < deadline:
            try:
                raw = capture.ws_recv(sock)
            except socket.timeout:
                continue
            except Exception:  # noqa: BLE001
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            method = msg.get("method")
            params = msg.get("params") or {}
            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                rid = params.get("requestId")
                pending[rid] = {
                    "rid": rid,
                    "method": req.get("method"),
                    "url": req.get("url"),
                    "postData": req.get("postData"),
                    "type": params.get("type"),
                }
            elif method == "Network.responseReceived":
                rid = params.get("requestId")
                resp = params.get("response") or {}
                rec = pending.pop(rid, None)
                if rec:
                    rec["status"] = resp.get("status")
                    rec["mime"] = resp.get("mimeType")
                    records.append(rec)
            elif method == "Network.loadingFailed":
                rid = params.get("requestId")
                rec = pending.pop(rid, None)
                if rec:
                    rec["status"] = "FAILED:" + str(params.get("errorText"))
                    records.append(rec)

        if bodies:
            for rec in records:
                u = rec.get("url") or ""
                if not any(k in u for k in INTEREST):
                    continue
                if "json" not in str(rec.get("mime") or ""):
                    continue
                try:
                    got = call("Network.getResponseBody", {"requestId": rec["rid"]})
                    body = (got.get("result") or {}).get("body") or ""
                    rec["body"] = body[:20000]
                except Exception:  # noqa: BLE001
                    continue
    finally:
        try:
            capture.ws_send(sock, "", opcode=0x8)
        except Exception:  # noqa: BLE001
            pass
        sock.close()
    for rec in pending.values():
        rec["status"] = "NO_RESPONSE"
        records.append(rec)
    return records

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="抓雨课堂页面自己发的接口")
    ap.add_argument("--url", required=True)
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--all", action="store_true", help="连静态资源也打印")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    port = ensure_browser(args.port)
    LOG.info("打开 %s，采集 %.0f 秒…", args.url, args.seconds)
    records = sniff(port, args.url, args.seconds)

    hits = [r for r in records
            if args.all or any(k in (r.get("url") or "") for k in INTEREST)]
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = OUT / f"{stamp}.json"
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2)[:800000], encoding="utf-8")

    print(f"\n共 {len(records)} 个请求，命中 {len(hits)} 个：\n")
    for r in hits:
        url = r.get("url") or ""
        short = url[len(capture.HOST):] if url.startswith(capture.HOST) else url
        line = f"{r.get('status'):>6} {r.get('method'):<5} {short[:150]}"
        print(line)
        if r.get("postData"):
            print("        body:", r["postData"][:300])
    print(f"\n全量已保存：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
