# -*- coding: utf-8 -*-
"""自动捕捉雨课堂 cookie —— 不用 F12 手抄。

三种模式：

    python capture.py                 # auto：先读本地浏览器库，失败再走浏览器调试端口
    python capture.py --mode cdp      # 拉起 Edge/Chrome 调试实例，你在窗口里登录，脚本自己抓
    python capture.py --mode db       # 直接解本地 Cookies 库（Chrome/Edge ≤126 的 v10 格式）
    python capture.py --check         # 只校验已有 cookie 是否还有效

抓到后写 cookie.txt，并（默认）同步写进 config.json 的 cookie_string，之后直接：
    python main.py --auto

原理
----
- cdp：起一个独立 profile 的浏览器（--remote-debugging-port），走 CDP 的
  `Storage.getCookies`，由浏览器自己解密，v10/v20/App-Bound 全都不影响；
  首次在一个新 profile 里登录一次，之后这个 profile 还能继续用。
- db：读 `<User Data>/<Profile>/Network/Cookies`（immutable 打开避开占用锁），
  Local State 的 os_crypt.encrypted_key 用 DPAPI 解出 32 字节主密钥，
  v10 值走 AES-256-GCM（node 内置 crypto 解密，无第三方依赖）。
  Edge/Chrome 127+ 的 App-Bound 加密（v20）解不了 → 自动转 cdp。
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from paths import app_dir as _app_dir
BASE = _app_dir()
COOKIE_FILE = BASE / "cookie.txt"
CONFIG_FILE = BASE / "config.json"
LOG = logging.getLogger("capture")

HOST = "https://changjiang.yuketang.cn"
DOMAIN_MATCH = ("yuketang", "xuetangx")

BROWSERS: List[Tuple[str, str]] = [
    ("Edge", r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ("Edge", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ("Chrome", r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    ("Chrome", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ("Chrome", r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ("Brave", r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
]

USER_DATA_DIRS: List[Tuple[str, str]] = [
    ("Edge", r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    ("Chrome", r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    ("Brave", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"),
]


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def find_node() -> Optional[str]:
    for cand in (
        os.environ.get("YKT_NODE"),
        r"%USERPROFILE%\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\node\bin\node.exe",
        r"C:\Program Files\nodejs\node.exe",
        shutil.which("node"),
    ):
        if not cand:
            continue
        path = _expand(cand)
        if os.path.exists(path) or shutil.which(path):
            return path
    return None


def find_powershell() -> Optional[str]:
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        p = shutil.which(name)
        if p:
            return p
    return None


def dpapi_unprotect(blob: bytes) -> bytes:
    """用 DPAPI(CurrentUser) 解 Chromium 的主密钥。"""
    ps = find_powershell()
    if not ps:
        raise RuntimeError("找不到 PowerShell，无法做 DPAPI 解密")
    b64 = base64.b64encode(blob).decode()
    script = (
        "Add-Type -AssemblyName System.Security;"
        "$b=[Convert]::FromBase64String('%s');"
        "$p=[System.Security.Cryptography.ProtectedData]::Unprotect("
        "$b,$null,[System.Security.Cryptography.DataProtectionScope]::CurrentUser);"
        "[Convert]::ToBase64String($p)" % b64
    )
    out = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"DPAPI 失败：{(out.stderr or '').strip()[:200]}")
    return base64.b64decode(out.stdout.strip())


def _aesgcm_script() -> Optional[str]:
    """tools/aesgcm.js 的位置：源码运行在项目目录，打包后在 _MEIPASS。"""
    for cand in (BASE / "tools" / "aesgcm.js",
                 Path(getattr(sys, "_MEIPASS", BASE)) / "tools" / "aesgcm.js"):
        if cand.exists():
            return str(cand)
    return None


def aes_gcm_decrypt(key: bytes, blob: bytes) -> str:
    """v10 值：12 字节 nonce + 密文 + 16 字节 tag。用 node 的 crypto 解。"""
    node = find_node()
    if not node:
        raise RuntimeError("找不到 node，无法解 AES-GCM（可 setup 一个 node，或改用 --mode cdp）")
    payload = json.dumps({
        "key": base64.b64encode(key).decode(),
        "blob": base64.b64encode(blob).decode(),
    })
    script = _aesgcm_script()
    if not script or not os.path.exists(script):
        raise RuntimeError("找不到 tools/aesgcm.js，db 路线不可用（改用 --mode cdp）")
    out = subprocess.run(
        [node, str(script)],
        input=payload, capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"AES-GCM 失败：{(out.stderr or '').strip()[:200]}")
    return out.stdout


# ---------------------------------------------------------------------------
# WebSocket（CDP 需要，纯标准库）
# ---------------------------------------------------------------------------
def ws_connect(ws_url: str, timeout: float = 10.0) -> socket.socket:
    u = urllib.parse.urlsplit(ws_url)
    host = u.hostname or "127.0.0.1"
    port = u.port or 80
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("WS 握手被关闭")
        buf += chunk
    if b"101" not in buf.split(b"\r\n")[0]:
        raise RuntimeError(f"WS 握手失败：{buf[:120]!r}")
    return sock


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("WS 连接断开")
        buf += chunk
    return buf


def ws_send(sock: socket.socket, text: str, opcode: int = 1) -> None:
    data = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytes([0x80 | opcode])
    size = len(data)
    if size < 126:
        header += bytes([0x80 | size])
    elif size < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", size)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", size)
    masked = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    sock.sendall(header + mask + masked)


def ws_recv(sock: socket.socket) -> str:
    while True:
        b1, b2 = _recv_exact(sock, 2)
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        masked = bool(b2 & 0x80)
        mask = _recv_exact(sock, 4) if masked else None
        data = _recv_exact(sock, length)
        if masked and mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        if opcode == 0x8:
            raise RuntimeError("WS 被对端关闭")
        if opcode == 0x9:
            ws_send(sock, data.decode("utf-8", "ignore"), opcode=0xA)
            continue
        if opcode == 0xA:
            continue
        return data.decode("utf-8", "replace")


def cdp_call(sock: socket.socket, mid: int, method: str, params: Optional[dict] = None) -> Dict[str, Any]:
    ws_send(sock, json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws_recv(sock))
        if msg.get("id") == mid:
            return msg


# ---------------------------------------------------------------------------
# CDP 路线
# ---------------------------------------------------------------------------
def debug_port_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def pick_port(preferred: int = 9222, span: int = 8) -> int:
    for p in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return preferred


def find_browser_exe(prefer: Optional[str] = None) -> Optional[Tuple[str, str]]:
    order = sorted(BROWSERS, key=lambda x: 0 if prefer and x[0].lower() == prefer.lower() else 1)
    for name, rel in order:
        path = _expand(rel)
        if os.path.exists(path):
            return name, path
    return None


def browser_running(prefer: Optional[str] = None) -> bool:
    images = ["msedge.exe", "chrome.exe", "brave.exe"]
    if prefer:
        images = [f"{prefer.lower()}.exe"] + [i for i in images if not i.startswith(prefer.lower())]
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20)
        lower = (out.stdout or "").lower()
        return any(img in lower for img in images)
    except Exception:  # noqa: BLE001
        return False


def real_user_data_dir(prefer: Optional[str] = None) -> Optional[Path]:
    order = sorted(USER_DATA_DIRS, key=lambda x: 0 if prefer and x[0].lower() == prefer.lower() else 1)
    for _, rel in order:
        root = Path(_expand(rel))
        if (root / "Local State").exists():
            return root
    return None


def cdp_get_cookies(port: int) -> List[Dict[str, Any]]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
        info = json.loads(r.read().decode("utf-8"))
    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("调试端口没给 webSocketDebuggerUrl")
    sock = ws_connect(ws_url)
    try:
        res = cdp_call(sock, 1, "Storage.getCookies")
    finally:
        try:
            ws_send(sock, "", opcode=0x8)
        except Exception:
            pass
        sock.close()
    cookies = (res.get("result") or {}).get("cookies") or []
    return [c for c in cookies if any(d in (c.get("domain") or "") for d in DOMAIN_MATCH)]


PORT_FILE = BASE / ".ykt-cdp-port"
EDGE_LOG = BASE / "dump" / "edge-launch.log"
DEDICATED_PROFILE = Path(_expand(r"%LOCALAPPDATA%\ykt-capture-profile"))


def save_port(port: int) -> None:
    try:
        PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def load_port() -> Optional[int]:
    try:
        if PORT_FILE.exists():
            return int(PORT_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return None


def pids_with_profile(profile_dir: Path) -> List[int]:
    """找出命令行里带这个 profile 目录的浏览器进程（PowerShell CIM，无需第三方库）。"""
    ps = find_powershell()
    if not ps:
        return []
    key = profile_dir.name or str(profile_dir)
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe' or Name='brave.exe'\""
        " | Where-Object { $_.CommandLine -like '*%s*' } | ForEach-Object { $_.ProcessId }" % key
    )
    try:
        out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=30)
        return [int(x) for x in (out.stdout or "").split() if x.isdigit()]
    except Exception:  # noqa: BLE001
        return []


def kill_stale_capture_browsers(profile_dir: Path) -> int:
    """上一个抓取实例可能还占着这个 profile：新进程会交给它然后不带调试端口。

    这也是「浏览器起来了但调试端口没开」最常见的原因，所以启动前先清掉。
    """
    pids = pids_with_profile(profile_dir)
    if not pids:
        return 0
    LOG.info("清理占用 profile 的残留浏览器进程：%s", pids)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=20)
    time.sleep(1.0)
    return len(pids)


def launch_debug_browser(exe: str, port: int, profile_dir: Path, url: str) -> subprocess.Popen:
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeFirstRunExperience,msImplicitSignin",
        url,
    ]
    LOG.info("启动浏览器：%s（profile=%s port=%d）", os.path.basename(exe), profile_dir, port)
    # pythonw 下父进程没有有效 stdio 句柄，Chromium 可能因此失败，统一重定向到日志
    try:
        EDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(EDGE_LOG, "a", encoding="utf-8", errors="replace")
    except OSError:
        log = subprocess.DEVNULL
    return subprocess.Popen(args, close_fds=True, stdout=log, stderr=subprocess.STDOUT)


def wait_for_port(port: int, proc: Optional[subprocess.Popen], timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if debug_port_alive(port):
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.4)
    return debug_port_alive(port)


def diagnose_launch(port: int, profile_dir: Path, label: str) -> str:
    bits = [f"[{label}] 调试端口 {port} 没开"]
    if profile_dir is not None:
        bits.append(f"profile={profile_dir}")
        locks = [n for n in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")
                 if (profile_dir / n).exists()]
        if locks:
            bits.append(f"目录里有锁文件 {locks}（可能有残留实例占用）")
    try:
        if EDGE_LOG.exists():
            tail = EDGE_LOG.read_text(encoding="utf-8", errors="replace")[-400:].strip()
            if tail:
                bits.append("浏览器日志尾部：" + tail.replace("\n", " "))
    except OSError:
        pass
    bits.append("可试：关掉 Edge/浏览器后重跑、或 python capture.py --fresh-profile、"
                "或手动 msedge.exe --remote-debugging-port=9222 --user-data-dir=%TEMP%\\ykt")
    return "；".join(bits)


def capture_cdp(
    port: int = 9222,
    prefer: Optional[str] = None,
    wait: float = 300.0,
    profile_dir: Optional[Path] = None,
    keep_open: bool = False,
    validate: bool = True,
    use_real_profile: Optional[bool] = None,
    fresh_profile: bool = False,
) -> Optional[Dict[str, str]]:
    proc: Optional[subprocess.Popen] = None

    # 0) 上一次成功启动的端口还活着就直接复用，避免反复拉起撞实例
    saved = load_port()
    if saved and debug_port_alive(saved):
        port = saved
        LOG.info("复用上次的调试端口 127.0.0.1:%d", port)
    if not debug_port_alive(port):
        found = find_browser_exe(prefer)
        if not found:
            raise RuntimeError("没找到 Edge/Chrome，装一个或手动开调试端口")
        name, exe = found
        running = browser_running(name)
        real = real_user_data_dir(name)

        attempts: List[tuple] = []
        if profile_dir is not None:
            attempts.append(("指定配置", Path(profile_dir)))
        elif fresh_profile:
            attempts.append(("全新配置", Path(tempfile.mkdtemp(prefix="ykt-cdp-"))))
        elif not running and real:
            attempts.append(("现有配置", real))
        else:
            attempts.append(("独立配置", DEDICATED_PROFILE))
        if not fresh_profile and profile_dir is None:
            attempts.append(("全新配置", Path(tempfile.mkdtemp(prefix="ykt-cdp-"))))

        last_error = ""
        for label, pdir in attempts:
            if label == "独立配置":
                kill_stale_capture_browsers(pdir)
            elif label == "现有配置" and browser_running(name):
                continue  # 浏览器跑着的时候用真实配置会被已有实例接管，必然没端口
            port = pick_port(port)
            try:
                proc = launch_debug_browser(exe, port, pdir, f"{HOST}/v2/web/index")
            except Exception as e:  # noqa: BLE001
                last_error = f"启动失败：{e}"
                proc = None
                continue
            if wait_for_port(port, proc):
                save_port(port)
                profile_dir = pdir
                LOG.info("调试端口已就绪：127.0.0.1:%d（%s）", port, label)
                break
            last_error = diagnose_launch(port, pdir, label)
            LOG.warning("%s", last_error)
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
            proc = None
            if label == "独立配置" and pids_with_profile(pdir):
                kill_stale_capture_browsers(pdir)
        else:
            raise RuntimeError(last_error or "浏览器起来了但调试端口没开")

        if profile_dir == DEDICATED_PROFILE and running:
            LOG.info("%s 正在运行，用的是独立配置：在新窗口登录一次即可（之后能复用）", name)
    else:
        LOG.info("复用已开调试端口的浏览器：127.0.0.1:%d", port)

    LOG.info("等待登录（最多 %.0f 秒）：在弹出的窗口里登录雨课堂，脚本会自动抓", wait)
    deadline = time.time() + wait
    last_names: List[str] = []
    while time.time() < deadline:
        try:
            cookies = cdp_get_cookies(port)
        except Exception as e:  # noqa: BLE001
            LOG.debug("取 cookie 失败：%s", e)
            time.sleep(1.5)
            continue
        names = sorted(c.get("name") for c in cookies)
        if names != last_names:
            LOG.info("当前命中域名 cookie：%s", names or "（空）")
            last_names = names
        jar = {c["name"]: c["value"] for c in cookies if c.get("value")}
        if "sessionid" in jar:
            if not validate or verify(jar)[0]:
                if proc and not keep_open:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                return jar
            LOG.info("拿到 sessionid 但服务端不认，继续等待登录刷新")
        time.sleep(2.0)
    if proc and not keep_open:
        try:
            proc.terminate()
        except Exception:
            pass
    LOG.error("等待超时，没抓到有效 sessionid")
    return None


# ---------------------------------------------------------------------------
# 本地 cookie 库路线
# ---------------------------------------------------------------------------
def load_master_key(user_data: Path) -> bytes:
    ls = user_data / "Local State"
    state = json.loads(ls.read_text(encoding="utf-8"))
    enc = state.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        raise RuntimeError("Local State 里没有 os_crypt.encrypted_key")
    raw = base64.b64decode(enc)
    if raw[:5] == b"DPAPI":
        raw = raw[5:]
    return dpapi_unprotect(raw)


def read_cookie_db(db: Path) -> List[Tuple[str, str, bytes]]:
    """immutable 打开，避开浏览器占用锁。"""
    uri = f"file:{db.as_posix()}?immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return con.execute(
            "select host_key, name, encrypted_value from cookies "
            "where host_key like '%yuketang%' or host_key like '%xuetangx%'"
        ).fetchall()
    finally:
        con.close()


def capture_db(prefer: Optional[str] = None, validate: bool = True,
               roots: Optional[List[Tuple[str, str]]] = None) -> Optional[Dict[str, str]]:
    order = sorted(roots or USER_DATA_DIRS,
                   key=lambda x: 0 if prefer and x[0].lower() == prefer.lower() else 1)
    for name, rel in order:
        root = Path(_expand(rel))
        if not (root / "Local State").exists():
            continue
        profiles = [p for p in root.iterdir() if p.is_dir() and (p / "Network" / "Cookies").exists()]
        if not profiles:
            continue
        try:
            key = load_master_key(root)
        except Exception as e:  # noqa: BLE001
            LOG.warning("[%s] 取主密钥失败：%s", name, e)
            continue
        for prof in sorted(profiles, key=lambda p: (p.name != "Default", p.name)):
            db = prof / "Network" / "Cookies"
            try:
                rows = read_cookie_db(db)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "unable to open database file" in msg:
                    LOG.info("[%s/%s] 库被浏览器独占（%s 正在运行）——关掉浏览器可走 db 路线，"
                             "或直接用 --mode cdp（不用关浏览器）", name, prof.name, name)
                else:
                    LOG.warning("[%s/%s] 读库失败：%s", name, prof.name, msg)
                continue
            if not rows:
                continue
            jar: Dict[str, str] = {}
            v20 = 0
            for host, cname, enc in rows:
                try:
                    if enc[:3] in (b"v10", b"v11"):
                        jar[cname] = aes_gcm_decrypt(key, enc[3:])
                    elif enc[:3] == b"v20":
                        v20 += 1
                    else:
                        jar[cname] = enc.decode("utf-8", "ignore")
                except Exception as e:  # noqa: BLE001
                    LOG.debug("[%s/%s] 解密 %s 失败：%s", name, prof.name, cname, e)
            LOG.info("[%s/%s] 命中 %d 条，解出 %d 条%s", name, prof.name, len(rows), len(jar),
                     f"，{v20} 条是 App-Bound(v20) 解不了" if v20 else "")
            if jar.get("sessionid"):
                if not validate or verify(jar)[0]:
                    return jar
                LOG.info("[%s/%s] sessionid 已失效，转 cdp 重新登录", name, prof.name)
    return None


# ---------------------------------------------------------------------------
# 校验 / 落盘
# ---------------------------------------------------------------------------
def verify(jar: Dict[str, str]) -> Tuple[bool, str]:
    cookie = "; ".join(f"{k}={v}" for k, v in jar.items() if k in (
        "sessionid", "csrftoken", "university_id", "platform_id", "uv_id"))
    if not cookie:
        return False, "没有可用 cookie"
    req = urllib.request.Request(
        f"{HOST}/v2/api/web/userinfo",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
            "referer": f"{HOST}/v2/web/index",
            "xtbz": "ykt",
            "xt-agent": "web",
            "x-csrftoken": jar.get("csrftoken", ""),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return False, f"请求失败：{e}"
    if str(data.get("errmsg", "")).lower() == "success" or data.get("data"):
        info = (data.get("data") or [{}])[0] if isinstance(data.get("data"), list) else {}
        return True, str(info.get("name") or info.get("user_id") or "ok")
    return False, json.dumps(data, ensure_ascii=False)[:160]


def save_cookies(jar: Dict[str, str], to_config: bool = True) -> Path:
    line = "; ".join(f"{k}={v}" for k, v in jar.items())
    COOKIE_FILE.write_text(line + "\n", encoding="utf-8")
    LOG.info("已写入 %s（%d 条）", COOKIE_FILE.name, len(jar))
    if to_config and CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except ValueError:
            cfg = {}
        cfg["cookie_string"] = line
        cfg.setdefault("cookies", {}).update({
            k: jar[k] for k in ("csrftoken", "sessionid", "university_id") if k in jar
        })
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("已同步 config.json.cookie_string")
    return COOKIE_FILE


# ---------------------------------------------------------------------------
def capture(
    mode: str = "auto",
    port: int = 9222,
    prefer: Optional[str] = None,
    wait: float = 300.0,
    keep_open: bool = False,
    validate: bool = True,
    fresh_profile: bool = False,
) -> Optional[Dict[str, str]]:
    if mode in ("auto", "db"):
        jar = capture_db(prefer=prefer, validate=validate)
        if jar:
            return jar
        if mode == "db":
            LOG.error("本地库没拿到可用 cookie（v20 App-Bound 或已过期），用 --mode cdp")
            return None
        LOG.info("本地库路线不可用，转浏览器调试端口")
    if mode in ("auto", "cdp"):
        return capture_cdp(port=port, prefer=prefer, wait=wait, keep_open=keep_open,
                           validate=validate, fresh_profile=fresh_profile)
    raise ValueError(f"未知模式 {mode}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="自动捕捉雨课堂 cookie")
    ap.add_argument("--mode", default="auto", choices=["auto", "cdp", "db"])
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--browser", default=None, help="Edge / Chrome")
    ap.add_argument("--wait", type=float, default=300, help="cdp 模式下等登录的秒数")
    ap.add_argument("--keep-open", action="store_true", help="抓完不关浏览器")
    ap.add_argument("--fresh-profile", action="store_true",
                    help="用全新临时 profile 启动（profile 被残留实例占用时用）")
    ap.add_argument("--no-config", action="store_true", help="只写 cookie.txt，不动 config.json")
    ap.add_argument("--check", action="store_true", help="只校验 cookie.txt 是否有效")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.check:
        if not COOKIE_FILE.exists():
            LOG.error("没有 cookie.txt")
            return 2
        jar = {}
        for chunk in COOKIE_FILE.read_text(encoding="utf-8").replace("\n", ";").split(";"):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                jar[k.strip()] = v.strip()
        ok, who = verify(jar)
        LOG.info("cookie %s：%s", "有效" if ok else "无效", who)
        return 0 if ok else 3

    jar = capture(mode=args.mode, port=args.port, prefer=args.browser,
                  wait=args.wait, keep_open=args.keep_open,
                  fresh_profile=args.fresh_profile)
    if not jar:
        return 4
    ok, who = verify(jar)
    LOG.info("校验：%s（%s）", "通过" if ok else "未通过", who)
    if not ok:
        return 4
    save_cookies(jar, to_config=not args.no_config)
    LOG.info("完成。直接跑：python main.py --auto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
