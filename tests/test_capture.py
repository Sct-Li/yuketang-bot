# -*- coding: utf-8 -*-
"""自测 capture.py 的两条路线，不碰真实浏览器数据。

1) db 路线：造一个临时 user-data 目录（Local State + v10 加密 cookie），
   走 DPAPI + node AES-GCM 解密，比对明文。
2) cdp 路线：headless 启 Edge 开调试端口，用 CDP 塞一条 cookie，再用我们的
   WS 客户端读回来。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capture  # noqa: E402


def node_encrypt(key: bytes, plaintext: str) -> bytes:
    """用 node 造 v10 值：nonce(12) + ct + tag(16)。"""
    code = (
        "const c=require('crypto');const i=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "const k=Buffer.from(i.key,'base64');const n=Buffer.alloc(12,7);"
        "const e=c.createCipheriv('aes-256-gcm',k,n);"
        "const out=Buffer.concat([e.update(i.pt,'utf8'),e.final()]);"
        "process.stdout.write(Buffer.concat([n,out,e.getAuthTag()]).toString('base64'));"
    )
    node = capture.find_node()
    out = subprocess.run([node, "-e", code],
                         input=json.dumps({"key": base64.b64encode(key).decode(), "pt": plaintext}),
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:200])
    return base64.b64decode(out.stdout)


def protect_key(key: bytes) -> bytes:
    ps = capture.find_powershell()
    b64 = base64.b64encode(key).decode()
    script = (
        "Add-Type -AssemblyName System.Security;"
        "$b=[Convert]::FromBase64String('%s');"
        "$p=[System.Security.Cryptography.ProtectedData]::Protect("
        "$b,$null,[System.Security.Cryptography.DataProtectionScope]::CurrentUser);"
        "[Convert]::ToBase64String($p)" % b64
    )
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout)[:200])
    return base64.b64decode(out.stdout.strip())


def test_db() -> bool:
    key = os.urandom(32)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "User Data"
        prof = root / "Default" / "Network"
        prof.mkdir(parents=True)
        blob = node_encrypt(key, "SESSION-VALUE-123")
        con = sqlite3.connect(prof / "Cookies")
        con.execute("create table cookies (host_key text, name text, encrypted_value blob)")
        con.execute("insert into cookies values (?,?,?)", (".yuketang.cn", "sessionid", b"v10" + blob))
        con.execute("insert into cookies values (?,?,?)", (".yuketang.cn", "csrftoken", b"v10" + node_encrypt(key, "CSRF-456")))
        con.execute("insert into cookies values (?,?,?)", (".example.com", "ignore", b"plain"))
        con.commit()
        con.close()
        state = {"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + protect_key(key)).decode()}}
        (root / "Local State").write_text(json.dumps(state), encoding="utf-8")

        jar = capture.capture_db(validate=False, roots=[("Test", str(root))])
        print("db route =>", jar)
        return bool(jar) and jar.get("sessionid") == "SESSION-VALUE-123" and jar.get("csrftoken") == "CSRF-456"


def find_edge() -> str | None:
    for name, rel in capture.BROWSERS:
        p = capture._expand(rel)
        if name == "Edge" and os.path.exists(p):
            return p
    return None


def test_cdp() -> bool:
    exe = find_edge()
    if not exe:
        print("cdp route => skipped (no Edge/Chrome found)")
        return True
    port = capture.pick_port(9411)
    profile = Path(tempfile.mkdtemp(prefix="ykt_cdp_"))
    proc = subprocess.Popen(
        [exe, "--headless=new", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            if capture.debug_port_alive(port):
                break
            time.sleep(0.5)
        if not capture.debug_port_alive(port):
            print("cdp route => FAILED (debug port never opened)")
            return False

        # 用 CDP 塞一条 cookie
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
            ws_url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
        sock = capture.ws_connect(ws_url)
        try:
            resp = capture.cdp_call(sock, 1, "Storage.setCookies", {"cookies": [{
                "name": "sessionid", "value": "CDP-SESSION-789",
                "domain": ".yuketang.cn", "path": "/", "secure": True,
            }]})
            print("setCookies =>", json.dumps(resp)[:120])
        finally:
            sock.close()

        jar = capture.cdp_get_cookies(port)
        print("cdp route =>", jar)
        got = {c["name"]: c["value"] for c in jar}
        return got.get("sessionid") == "CDP-SESSION-789"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    ok1 = test_db()
    ok2 = test_cdp()
    print(f"\ndb={ok1} cdp={ok2}")
    sys.exit(0 if (ok1 and ok2) else 1)
