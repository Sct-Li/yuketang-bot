# -*- coding: utf-8 -*-
"""雨课堂刷课助手 —— 图形界面（tkinter，零第三方依赖，可打包成 exe）。

    python gui.py            # 源码运行
    雨课堂刷课助手.exe        # 打包后运行（同一套代码，路径由 paths.py 适配）

流程：① 自动抓 Cookie → ② 读取课程 → 勾选 → ③ 开始刷课（后台线程，可随时停止）
"""
from __future__ import annotations

import json
import logging
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

import paths
from paths import APP_NAME, APP_VERSION

import capture
import player as pl
from ykt import HOST, AuthError, Yuketang

BASE = paths.app_dir()
CFG = BASE / "config.json"
PROFILE = BASE / "profile.json"

# ------------------------------------------------------------------ 配色
BG = "#f4f5f7"
CARD = "#ffffff"
BORDER = "#e3e6eb"
TEXT = "#111827"
MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_DK = "#1d4ed8"
OK = "#15803d"
WARN = "#b45309"
DANGER = "#b91c1c"
LOG_BG = "#0b1020"

COLS = [
    ("name", "课程", 360),
    ("classroom", "classroom_id", 110),
    ("chapters", "章", 44),
    ("leaves", "知识点", 66),
    ("videos", "视频", 46),
    ("done", "已完成", 60),
    ("kind", "类型", 78),
    ("duration", "总时长", 76),
    ("state", "状态", 76),
]

KIND_TEXT = {"mooc": "在线课", "activity": "活动兜底", "graphspace": "学习空间",
             "classroom": "课堂型", "unknown": "?", "": "?"}

STATE_TAG = {"可刷": "ready", "已完成": "done", "无内容": "empty",
             "错误": "error", "待扫描": "pending"}


class QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(("log", (record.levelno, self.format(record))))
        except Exception:  # noqa: BLE001
            pass


class App(tk.Tk):
    def __init__(self, auto_load: bool = False) -> None:
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.configure(bg=BG)
        self.minsize(1020, 660)
        try:
            self.tk.call("tk", "scaling", 1.0)
        except tk.TclError:
            pass

        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.client: Optional[Yuketang] = None
        self.courses: List[Dict[str, Any]] = []
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.stop_event = threading.Event()
        self.busy = False
        self.leaf_total = 0
        self.leaf_done = 0
        _cfg0 = self._cfg()
        self.workers = max(1, min(8, int(_cfg0.get("workers", 5) or 5)))
        self.cache_ttl = int(_cfg0.get("cache_ttl", 900) or 0)
        self._scan_clients: list = []
        self._scan_pool: list = []
        self._scan_lock = threading.Lock()
        self._scan_cache: Dict[str, Any] = {}
        self.auto_load = auto_load

        self._load_cfg()
        self._setup_style()
        self._setup_logging()
        self._build_ui()
        self._restore_geometry()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(120, self._pump)
        self.log_info("就绪。点「① 自动抓 Cookie」→「② 读取课程」→ 勾选 →「③ 开始刷课」")
        self.after(800, self._dump_layout)
        if auto_load:
            self.after(500, self.on_load)

    # ------------------------------------------------------------ 配置
    def _cfg(self) -> Dict[str, Any]:
        if CFG.exists():
            try:
                return json.loads(CFG.read_text(encoding="utf-8-sig"))
            except ValueError:
                return {}
        return {}

    def _load_cfg(self) -> None:
        cfg = self._cfg()
        self.ui_cfg = cfg

    def _save_ui_cfg(self) -> None:
        cfg = self._cfg()
        cfg.update({
            "step": int(self.var_step.get()),
            "batch": int(self.var_batch.get()),
            "limit": int(self.var_limit.get()),
            "realtime": bool(self.var_realtime.get()),
            "with_slide": bool(self.var_slide.get()),
            "deep_scan": bool(self.chk_deep.get()),
            "geometry": self.geometry(),
        })
        try:
            CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _restore_geometry(self) -> None:
        geo = (self.ui_cfg or {}).get("geometry") or "1440x860+20+20"
        try:
            self.geometry(geo)
        except tk.TclError:
            self.geometry("1440x860+20+20")

    # ------------------------------------------------------------ 样式
    def _setup_style(self) -> None:
        st = ttk.Style(self)
        for theme in ("clam", "vista", "default"):
            if theme in st.theme_names():
                st.theme_use(theme)
                break
        st.configure(".", background=BG, foreground=TEXT,
                     font=("Microsoft YaHei UI", -13))
        st.configure("Card.TFrame", background=CARD)
        st.configure("Bg.TFrame", background=BG)
        st.configure("Header.TLabel", background=ACCENT, foreground="#ffffff",
                     font=("Microsoft YaHei UI", -20, "bold"))
        st.configure("HeaderSub.TLabel", background=ACCENT, foreground="#dbeafe",
                     font=("Microsoft YaHei UI", -13))
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        st.configure("Status.TLabel", background=BG, foreground=MUTED)
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                     padding=(14, 7), font=("Microsoft YaHei UI", -13, "bold"),
                     borderwidth=0, focusthickness=0)
        st.map("Accent.TButton",
               background=[("active", ACCENT_DK), ("disabled", "#9aa5b1")],
               foreground=[("disabled", "#f3f4f6")])
        st.configure("Ghost.TButton", background=CARD, foreground=TEXT,
                     padding=(10, 6), borderwidth=1, relief="solid")
        st.map("Ghost.TButton", background=[("active", "#eef2ff")])
        st.configure("Danger.TButton", background="#fee2e2", foreground=DANGER,
                     padding=(10, 6), borderwidth=1)
        st.map("Danger.TButton", background=[("active", "#fecaca")])
        st.configure("Treeview", background=CARD, fieldbackground=CARD,
                     foreground=TEXT, rowheight=29, borderwidth=0,
                     font=("Microsoft YaHei UI", -13))
        st.configure("Treeview.Heading", background="#f0f2f5", foreground=MUTED,
                     font=("Microsoft YaHei UI", -13, "bold"), relief="flat", padding=(6, 6))
        st.map("Treeview.Heading", background=[("active", "#e5e9f0")])
        st.map("Treeview", background=[("selected", "#dbeafe")],
               foreground=[("selected", "#1e3a8a")])
        st.configure("TCheckbutton", background=CARD, foreground=TEXT)
        st.configure("TSpinbox", fieldbackground="#ffffff")
        st.configure("TProgressbar", background=ACCENT, troughcolor="#e5e7eb",
                     borderwidth=0, thickness=12)

    # ------------------------------------------------------------ 日志
    def _setup_logging(self) -> None:
        handler = QueueLogHandler(self.q)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(handler)

    def log_info(self, text: str) -> None:
        self.q.put(("log", (logging.INFO, f"{time.strftime('%H:%M:%S')}  {text}")))

    # ------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # 顶部品牌条
        head = tk.Frame(self, bg=ACCENT, height=58)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text=f"  {APP_NAME}", bg=ACCENT, fg="#ffffff",
                 font=("Microsoft YaHei UI", -22, "bold")).pack(side="left", padx=(12, 0), pady=10)
        tk.Label(head, text=f"v{APP_VERSION} · 自动识别接口 / 学习空间课程 / 断点续刷",
                 bg=ACCENT, fg="#dbeafe",
                 font=("Microsoft YaHei UI", -13)).pack(side="left", padx=14)
        self.lbl_login = tk.Label(head, text="未登录", bg="#1e40af", fg="#e5e7eb",
                                  font=("Microsoft YaHei UI", -13, "bold"), padx=12, pady=4)
        self.lbl_login.pack(side="right", padx=14)

        # 工具条
        bar = ttk.Frame(self, style="Bg.TFrame")
        bar.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Button(bar, text="① 自动抓 Cookie", style="Accent.TButton",
                   command=self.on_capture).pack(side="left")
        ttk.Button(bar, text="校验登录", style="Ghost.TButton",
                   command=self.on_check).pack(side="left", padx=6)
        ttk.Button(bar, text="② 读取课程  (F5)", style="Ghost.TButton",
                   command=self.on_load).pack(side="left", padx=(10, 0))
        self.chk_deep = tk.BooleanVar(value=bool((self.ui_cfg or {}).get("deep_scan")))
        ttk.Checkbutton(bar, text="深度扫描", variable=self.chk_deep,
                        style="TCheckbutton").pack(side="left", padx=10)
        ttk.Button(bar, text="全选", style="Ghost.TButton",
                   command=lambda: self._select_all(True)).pack(side="right")
        ttk.Button(bar, text="反选", style="Ghost.TButton",
                   command=lambda: self._select_all(False)).pack(side="right", padx=6)

        # 表格卡片（占满中间剩余空间）
        card = ttk.Frame(self, style="Card.TFrame")
        card.pack(fill="both", expand=True, padx=12, pady=(4, 4))
        wrap = ttk.Frame(card, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=1, pady=1)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in COLS], show="headings",
                                 selectmode="extended", height=6)
        for key, title, width in COLS:
            self.tree.heading(key, text=title)
            anchor = "w" if key == "name" else "center"
            self.tree.column(key, width=width, minwidth=40, anchor=anchor,
                             stretch=False)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.tag_configure("ready", foreground=OK)
        self.tree.tag_configure("done", foreground=MUTED)
        self.tree.tag_configure("empty", foreground="#9ca3af")
        self.tree.tag_configure("error", foreground=DANGER)
        self.tree.tag_configure("pending", foreground=TEXT)
        self.tree.bind("<Double-1>", self._on_row_double)
        self.tree.bind("<Button-3>", self._on_row_menu)


        # 参数行
        opt = ttk.Frame(self, style="Bg.TFrame")
        opt.pack(fill="x", padx=12, pady=(6, 2))
        ttk.Label(opt, text="心跳秒", style="Status.TLabel").pack(side="left")
        self.var_step = tk.IntVar(value=int((self.ui_cfg or {}).get("step", 20) or 20))
        ttk.Spinbox(opt, from_=1, to=120, width=5, textvariable=self.var_step).pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="批大小", style="Status.TLabel").pack(side="left")
        self.var_batch = tk.IntVar(value=int((self.ui_cfg or {}).get("batch", 50) or 50))
        ttk.Spinbox(opt, from_=1, to=200, width=5, textvariable=self.var_batch).pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="每课上限", style="Status.TLabel").pack(side="left")
        self.var_limit = tk.IntVar(value=int((self.ui_cfg or {}).get("limit", 0) or 0))
        ttk.Spinbox(opt, from_=0, to=999, width=5, textvariable=self.var_limit).pack(side="left", padx=(4, 12))
        self.var_realtime = tk.BooleanVar(value=bool((self.ui_cfg or {}).get("realtime", True)))
        ttk.Checkbutton(opt, text="真速推进（稳）", variable=self.var_realtime,
                        style="TCheckbutton").pack(side="left", padx=(0, 12))
        self.var_slide = tk.BooleanVar(value=bool((self.ui_cfg or {}).get("with_slide", False)))
        ttk.Checkbutton(opt, text="含课件幻灯片", variable=self.var_slide,
                        style="TCheckbutton").pack(side="left")

        # 操作行
        act = ttk.Frame(self, style="Bg.TFrame")
        act.pack(fill="x", padx=12, pady=(4, 6))
        self.btn_start = ttk.Button(act, text="③ 开始刷课  (F9)", style="Accent.TButton",
                                    command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(act, text="停止 (Esc)", style="Danger.TButton",
                                   command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.pb = ttk.Progressbar(act, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True, padx=(12, 10))
        self.lbl_prog = ttk.Label(act, text="0 / 0", style="Status.TLabel", width=12, anchor="e")
        self.lbl_prog.pack(side="left")

        # 日志卡片
        logcard = ttk.Frame(self, style="Card.TFrame")
        logcard.pack(fill="x", padx=12, pady=(0, 6))
        loghead = ttk.Frame(logcard, style="Card.TFrame")
        loghead.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(loghead, text="运行日志", style="Card.TLabel").pack(side="left")
        ttk.Button(loghead, text="清空", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")
        self.var_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(loghead, text="自动滚动", variable=self.var_autoscroll,
                        style="TCheckbutton").pack(side="right", padx=8)
        ttk.Button(loghead, text="保存日志", style="Ghost.TButton",
                   command=self._save_log).pack(side="right")
        body = ttk.Frame(logcard, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.txt = tk.Text(body, height=5, wrap="none", state="disabled", bd=0,
                           bg=LOG_BG, fg="#e5e7eb", insertbackground="#e5e7eb",
                           font=("Consolas", -13), padx=10, pady=8)
        tsb = ttk.Scrollbar(body, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=tsb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.txt.tag_configure("INFO", foreground="#cbd5e1")
        self.txt.tag_configure("WARN", foreground="#fbbf24")
        self.txt.tag_configure("ERROR", foreground="#f87171")
        self.txt.tag_configure("OK", foreground="#4ade80")


        # 状态栏
        self.lbl_status = ttk.Label(self, text="空闲", style="Status.TLabel", anchor="w")
        self.lbl_status.pack(fill="x", padx=14, pady=(0, 8))

    def _bind_keys(self) -> None:
        self.bind("<F5>", lambda e: self.on_load())
        self.bind("<F9>", lambda e: self.on_start())
        self.bind("<Escape>", lambda e: self.on_stop())
        self.bind("<Control-a>", lambda e: self._select_all(True))
        self.bind("<Control-l>", lambda e: self._clear_log())

    # ------------------------------------------------------------ 日志实现
    def _append_log(self, level: int, text: str) -> None:
        tag = "INFO"
        if level >= logging.ERROR:
            tag = "ERROR"
        elif level >= logging.WARNING:
            tag = "WARN"
        elif "完成" in text or "✅" in text:
            tag = "OK"
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", tag)
        if self.var_autoscroll.get():
            self.txt.see("end")
        self.txt.configure(state="disabled")

    def _clear_log(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _save_log(self) -> None:
        out = BASE / "dump" / f"log-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.txt.get("1.0", "end"), encoding="utf-8")
        self.log_info(f"日志已保存：{out}")

    # ------------------------------------------------------------ 队列泵
    def _dump_layout(self) -> None:
        """把实际布局写进 dump/layout.txt，便于排查高 DPI 下的错位。"""
        try:
            kids = ",".join(f"{w.winfo_name()}={w.winfo_height()}" for w in self.winfo_children())
            info = (f"win={self.winfo_width()}x{self.winfo_height()} "
                    f"req={self.winfo_reqwidth()}x{self.winfo_reqheight()} "
                    f"scaling={self.tk.call('tk', 'scaling')} geom={self.geometry()} kids={kids}")
            out = BASE / "dump"
            out.mkdir(parents=True, exist_ok=True)
            (out / "layout.txt").write_text(info, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    if isinstance(payload, tuple):
                        self._append_log(payload[0], payload[1])
                    else:
                        self._append_log(logging.INFO, str(payload))
                elif kind == "courses":
                    self._fill_courses(payload)
                elif kind == "row":
                    self._update_row(payload)
                elif kind == "status":
                    self.lbl_status.configure(text=payload)
                elif kind == "login":
                    text, color = payload
                    self.lbl_login.configure(text=text)
                elif kind == "progress":
                    done, total = payload
                    self.pb.configure(maximum=max(total, done, 1), value=done)
                    pct = 0 if not total else int(done * 100 / total)
                    self.lbl_prog.configure(text=f"{done} / {total}  ({pct}%)")
                elif kind == "busy":
                    self._set_busy(payload)
                elif kind == "beat":
                    idx, n, name, pos, total = payload
                    pct = 0 if not total else int(pos * 100 / total)
                    self.lbl_status.configure(
                        text=f"[{idx}/{n}] 《{name}》 {pos}/{total}s ({pct}%)")
                elif kind == "reload":
                    self.on_load()
        except queue.Empty:
            pass
        self.after(110, self._pump)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.btn_start.configure(state="disabled" if busy else "normal")
        self.btn_stop.configure(state="normal" if busy else "disabled")

    # ------------------------------------------------------------ 表格
    def _fill_courses(self, courses: List[Dict[str, Any]]) -> None:
        self.courses = courses
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        for c in courses:
            cid = str(c["classroom_id"])
            row = {"name": c.get("name") or "", "classroom": cid, "chapters": "",
                   "leaves": "", "videos": "", "done": "", "kind": "", "duration": "",
                   "state": "待扫描", "_raw": {}}
            self.rows[cid] = row
            self.tree.insert("", "end", iid=cid, values=[row[k] for k, _, _ in COLS],
                             tags=("pending",))

    def _update_row(self, info: Dict[str, Any]) -> None:
        cid = str(info.get("classroom_id"))
        if cid not in self.rows:
            return
        row = self.rows[cid]
        leaves = int(info.get("leaves") or 0)
        done = int(info.get("done") or 0)
        if info.get("error"):
            state = "错误"
        elif not leaves:
            state = "无内容"
        elif done >= leaves:
            state = "已完成"
        else:
            state = "可刷"
        row.update({
            "chapters": info.get("chapters", ""),
            "leaves": leaves or "",
            "videos": info.get("videos", ""),
            "done": done if leaves else "",
            "kind": KIND_TEXT.get(info.get("kind", ""), info.get("kind", "")),
            "duration": _fmt_duration(info.get("duration") or 0),
            "state": state,
            "_raw": info,
        })
        self.tree.item(cid, values=[row[k] for k, _, _ in COLS],
                       tags=(STATE_TAG.get(state, "pending"),))

    def _select_all(self, on: bool) -> None:
        items = self.tree.get_children()
        self.tree.selection_set(items) if on else self.tree.selection_remove(items)

    def _selected(self) -> List[Dict[str, Any]]:
        out = []
        for iid in self.tree.selection():
            row = self.rows.get(iid)
            if row:
                out.append({"classroom_id": iid, "name": row["name"]})
        return out

    def _on_row_double(self, _event) -> None:
        sel = self.tree.selection()
        if sel:
            webbrowser.open(f"{HOST}/v2/web/studentLog/{sel[0]}")

    def _on_row_menu(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        self.tree.selection_set(row_id)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="只刷这门课", command=self.on_start)
        menu.add_command(label="打开课程页",
                         command=lambda: webbrowser.open(f"{HOST}/v2/web/studentLog/{row_id}"))
        menu.add_command(label="复制 classroom_id",
                         command=lambda: (self.clipboard_clear(), self.clipboard_append(row_id)))
        menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------ 登录
    def _make_client(self) -> Optional[Yuketang]:
        cfg = self._cfg()
        raw = cfg.get("cookie_string") or ""
        if not raw and (BASE / "cookie.txt").exists():
            raw = (BASE / "cookie.txt").read_text(encoding="utf-8-sig").strip()
        jar: Dict[str, str] = {}
        for chunk in str(raw).replace("\n", ";").split(";"):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                jar[k.strip()] = v.strip()
        jar.update({k: v for k, v in (cfg.get("cookies") or {}).items() if v})
        if not jar.get("csrftoken") or not jar.get("sessionid"):
            return None
        return Yuketang(jar["csrftoken"], jar["sessionid"],
                        jar.get("university_id") or cfg.get("uv_id") or "",
                        host=cfg.get("host") or HOST, profile_path=PROFILE)

    def on_capture(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.q.put(("status", "正在抓 cookie…浏览器窗口弹出后在里面登录即可"))
        threading.Thread(target=self._capture_worker, daemon=True).start()

    def _capture_worker(self) -> None:
        try:
            jar = capture.capture(mode="auto", validate=True)
            if not jar:
                self.q.put(("status", "没抓到 cookie（超时或没登录）"))
                self.q.put(("login", ("未登录", "#1e40af")))
                return
            capture.save_cookies(jar)
            ok, who = capture.verify(jar)
            self.q.put(("login", (f"已登录：{who}" if ok else "登录态异常", "#1e40af")))
            self.q.put(("status", "cookie 已保存，点「② 读取课程」"))
            self.client = self._make_client()
        except Exception as e:  # noqa: BLE001
            logging.getLogger("gui").exception("抓 cookie 失败：%s", e)
            self.q.put(("status", f"抓 cookie 失败：{e}"))
        finally:
            self.q.put(("busy", False))

    def on_check(self) -> None:
        client = self._make_client()
        if not client:
            self.q.put(("login", ("未登录（没有 cookie）", "#1e40af")))
            self.log_info("没有 cookie：先点「① 自动抓 Cookie」")
            return
        ok, who = capture.verify({
            "csrftoken": client.csrftoken, "sessionid": client.sessionid,
            "university_id": client.uv_id or "",
        })
        self.q.put(("login", (f"已登录：{who}" if ok else f"失效：{who}", "#1e40af")))
        self.log_info(("登录有效：" if ok else "登录无效：") + str(who))

    def _heal(self, why: str = "登录态失效") -> bool:
        self.q.put(("log", (logging.WARNING, f"{why}，自动重新抓 cookie…")))
        self.q.put(("status", "登录态失效，正在重新抓 cookie…"))
        try:
            jar = capture.capture(mode="auto", validate=True, wait=120)
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", (logging.ERROR, f"重新抓 cookie 失败：{e}")))
            return False
        if not jar:
            self.q.put(("log", (logging.ERROR, "没抓到新 cookie：点「① 自动抓 Cookie」手动登录一次")))
            return False
        capture.save_cookies(jar)
        old = self.client
        new = self._make_client()
        if not new:
            return False
        if old is not None:
            new.endpoints = dict(getattr(old, "endpoints", {}) or {})
        self.client = new
        self.q.put(("log", (logging.INFO, "已换上新 cookie，继续")))
        self.q.put(("login", ("已登录（已自动续期）", "#1e40af")))
        return True

    # ------------------------------------------------------------ 读取课程

    # ------------------------------------------------------------ 并发与缓存
    def _scan_client(self) -> Optional[Yuketang]:
        """每个扫描线程一个独立 client：Yuketang 有 cid/sign 等状态，共享会串味。"""
        with self._scan_lock:
            if self._scan_pool:
                return self._scan_pool.pop()
            if len(self._scan_clients) < self.workers:
                client = self._make_client()
                if client is not None:
                    client.profile_path = None      # 避免多线程同时写 profile.json
                    self._scan_clients.append(client)
                    return client
        return self.client

    def _release_scan_client(self, client: Optional[Yuketang]) -> None:
        if client is None or client is self.client:
            return
        with self._scan_lock:
            if client not in self._scan_pool and len(self._scan_pool) < self.workers:
                self._scan_pool.append(client)

    def _scan_one(self, course: Dict[str, Any], deep: bool) -> Dict[str, Any]:
        client = self._scan_client()
        try:
            return pl.scan_course(client, course, deep=deep, stop_event=self.stop_event)
        finally:
            self._release_scan_client(client)

    def _cache_file(self) -> Path:
        return BASE / "scan_cache.json"

    def _load_scan_cache(self) -> Dict[str, Dict[str, Any]]:
        if self.cache_ttl <= 0:
            return {}
        try:
            raw = json.loads(self._cache_file().read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        now = time.time()
        out: Dict[str, Dict[str, Any]] = {}
        for cid, item in (raw or {}).items():
            if not isinstance(item, dict):
                continue
            if now - float(item.get("ts") or 0) > self.cache_ttl:
                continue
            info = item.get("info") or {}
            if info:
                out[str(cid)] = info
        self._scan_cache = dict(out)
        return out

    def _remember_scan(self, info: Dict[str, Any]) -> None:
        cid = str(info.get("classroom_id"))
        if not cid:
            return
        self._scan_cache[cid] = info

    def _save_scan_cache(self) -> None:
        if self.cache_ttl <= 0 or not self._scan_cache:
            return
        payload = {cid: {"ts": time.time(), "info": info}
                   for cid, info in self._scan_cache.items()}
        try:
            self._cache_file().write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        except OSError:
            pass

    def on_load(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        deep = bool(self.chk_deep.get())
        threading.Thread(target=self._load_worker, args=(deep,), daemon=True).start()

    def _load_worker(self, deep: bool) -> None:
        try:
            self.client = self._make_client()
            if not self.client:
                self.q.put(("status", "没有 cookie，先点「① 自动抓 Cookie」"))
                self.q.put(("busy", False))
                return
            self.q.put(("status", "识别接口…"))
            try:
                self.client.detect(redetect=deep)
                courses = self.client.list_courses()
            except AuthError:
                self.q.put(("login", ("cookie 失效，自动重抓…", "#1e40af")))
                if not self._heal("读取课程时 401"):
                    self.q.put(("status", "重新抓 cookie 失败"))
                    return
                try:
                    self.client.detect()
                    courses = self.client.list_courses()
                except AuthError:
                    self.q.put(("status", "换 cookie 后仍然 401，请点「① 自动抓 Cookie」"))
                    return
            if not courses:
                self.q.put(("status", "课程列表为空"))
                return
            self.q.put(("login", (f"已登录：{self.client.login_name or ''}", "#1e40af")))
            self.q.put(("courses", courses))
            self.q.put(("status", f"共 {len(courses)} 门课，正在扫描章节…"))
            # 先把手上的缓存贴上去（秒开），再后台并发刷新
            cached = self._load_scan_cache()
            if cached:
                for c in courses:
                    hit = cached.get(str(c["classroom_id"]))
                    if hit:
                        self.q.put(("row", hit))
                self.q.put(("log", (logging.INFO, f"先用缓存填了 {len(cached)} 门课，后台刷新中…")))

            total = len(courses)
            done = 0
            t_start = time.time()
            self.q.put(("status", f"共 {total} 门课，{self.workers} 线程并发扫描…"))
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futs = {}
                for c in courses:
                    if self.stop_event.is_set():
                        break
                    futs[ex.submit(self._scan_one, c, deep)] = c
                for fut in as_completed(futs):
                    c = futs[fut]
                    done += 1
                    if self.stop_event.is_set():
                        continue
                    try:
                        info = fut.result()
                    except AuthError:
                        self.q.put(("log", (logging.WARNING, f"{c['name']} 返回 401，尝试自愈…")))
                        if not self._heal("扫描中 401"):
                            self.q.put(("status", "扫描中断：登录态无法恢复"))
                            break
                        try:
                            info = self._scan_one(c, deep)
                        except Exception as e2:  # noqa: BLE001
                            self.q.put(("log", (logging.ERROR, f"{c['name']} 仍然失败：{e2}")))
                            continue
                    except Exception as e:  # noqa: BLE001
                        self.q.put(("log", (logging.ERROR, f"{c['name']} 扫描异常：{e}")))
                        continue
                    info["classroom_id"] = str(c["classroom_id"])
                    info["name"] = c.get("name") or info.get("name") or ""
                    self._remember_scan(info)
                    self.q.put(("row", info))
                    self.q.put(("log", (logging.INFO,
                                        f"[{done}/{total}] {c['name']}：{info['chapters']} 章 / "
                                        f"{info['leaves']} 知识点，已完成 {info['done']}"
                                        + (f"，总时长 {info['duration']:.0f}s" if info["duration"] else "")
                                        + (f"，错误 {info['error']}" if info["error"] else ""))))
                    self.q.put(("status", f"扫描 {done}/{total}（{time.time() - t_start:.0f}s）"))
            self._save_scan_cache()
            self.q.put(("status", f"扫描完成（{time.time() - t_start:.1f}s）：勾选课程后点「③ 开始刷课」"))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("gui").exception("读取失败：%s", e)
            self.q.put(("status", f"读取失败：{e}"))
        finally:
            self.q.put(("busy", False))

    # ------------------------------------------------------------ 刷课
    def on_start(self) -> None:
        if self.busy:
            return
        sel = self._selected()
        if not sel:
            messagebox.showinfo("提示", "先在列表里选中要刷的课程（Ctrl/Shift 可多选，或点「全选」）")
            return
        if not self.client:
            self.client = self._make_client()
        if not self.client:
            messagebox.showwarning("提示", "还没有登录，先点「① 自动抓 Cookie」")
            return
        self.stop_event.clear()
        self.leaf_done, self.leaf_total = 0, 0
        self.q.put(("progress", (0, 1)))
        self._set_busy(True)
        args = (sel, int(self.var_step.get()), int(self.var_batch.get()),
                bool(self.var_realtime.get()), bool(self.var_slide.get()), int(self.var_limit.get()))
        threading.Thread(target=self._run_worker, args=args, daemon=True).start()

    def _run_worker(self, sel: List[Dict[str, Any]], step: int, batch: int,
                    realtime: bool, slide: bool, limit: int) -> None:
        total = {"done": 0, "skipped": 0, "failed": 0, "stopped": 0, "marked": 0}

        def on_leaf(idx: int, n: int, name: str, result: str) -> None:
            self.leaf_done += 1
            self.q.put(("progress", (self.leaf_done, max(self.leaf_total, 1))))

        def _on_beat(idx: int, n: int, name: str, pos: int, total: int) -> None:
            self.q.put(("beat", (idx, n, name, pos, total)))

        try:
            for ci, course in enumerate(sel, 1):
                if self.stop_event.is_set():
                    break
                self.q.put(("status", f"[{ci}/{len(sel)}] 正在刷《{course['name']}》"))
                raw = (self.rows.get(str(course["classroom_id"])) or {}).get("_raw") or {}
                pending = int(raw.get("videos") or 0) - int(raw.get("done") or 0)
                self.leaf_total += max(pending, 0)
                self.q.put(("progress", (self.leaf_done, max(self.leaf_total, 1))))
                stats = None
                for attempt in (1, 2):
                    try:
                        stats = pl.run_course(
                            self.client, course,
                            with_slide=slide, step=step, batch_size=batch,
                            realtime=realtime, limit=limit,
                            stop_event=self.stop_event, on_leaf=on_leaf,
                            on_beat=_on_beat,
                        )
                        break
                    except AuthError:
                        if attempt == 2 or not self._heal("刷课中 401"):
                            raise
                        self.q.put(("log", (logging.WARNING, f"《{course['name']}》换上新 cookie 后重试")))
                if stats is None:
                    continue
                for k in total:
                    total[k] += int(stats.get(k, 0))
                self.q.put(("log", (logging.INFO, f"《{course['name']}》结果：{stats}")))
            self.q.put(("status", "完成：done={done} skipped={skipped} failed={failed} marked={marked}"
                                 .format(**total)))
            if not self.stop_event.is_set():
                self.q.put(("reload", None))
        except AuthError as e:
            self.q.put(("log", (logging.ERROR, f"登录态失效且自愈失败：{e}")))
            self.q.put(("status", "刷课中断：登录态无法恢复"))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("gui").exception("刷课异常：%s", e)
            self.q.put(("status", f"刷课异常：{e}"))
        finally:
            self.q.put(("busy", False))

    def on_stop(self) -> None:
        if not self.busy:
            return
        self.stop_event.set()
        self.q.put(("status", "已发停止信号，当前批次结束后停"))

    # ------------------------------------------------------------ 关闭
    def _on_close(self) -> None:
        try:
            self._save_ui_cfg()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.stop_event.set()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


def _fmt_duration(sec: float) -> str:
    sec = int(sec or 0)
    if sec <= 0:
        return "-"
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec % 60:02d}s"


def selftest() -> int:
    """--selftest：只读自检。cookie → 章节 → 时长四路兜底，全程不发心跳、不写进度。

    结果写 dump/selftest.txt，同时在控制台打印。
    """
    paths.install_std_streams()
    app = App(auto_load=False)
    lines: List[str] = []

    def run() -> None:
        try:
            client = app._make_client()
            if not client:
                lines.append("没有 cookie：先跑一次「① 自动抓 Cookie」")
                return
            client.detect()
            lines.append(f"登录：{client.login_name}  uv={client.uv_id}  user_id={client.user_id}")
            courses = client.list_courses()
            lines.append(f"课程数：{len(courses)}")
            checked = 0
            for course in courses:
                if checked >= 3:
                    break
                client.set_course(course["classroom_id"])
                meta = client.classroom_meta()
                leaves = client.iter_leaves(client.chapters())
                if not leaves:
                    continue
                checked += 1
                lines.append(f"\n《{course['name']}》classroom={course['classroom_id']} "
                             f"sign={meta.get('sign')} 章节知识点={len(leaves)}")
                vids = 0
                for leaf in leaves:
                    if vids >= 3:
                        break
                    info = client.leaf_info(leaf["leaf_id"])
                    m = client.classify(leaf, info)
                    if m["kind"] != "video":
                        continue
                    leaf.update(m)
                    prog = client.progress(leaf["leaf_id"])
                    dur, src = pl.resolve_duration(client, leaf, float(m["duration"]), [], prog)
                    vids += 1
                    lines.append(f"  {leaf['leaf_id']} {leaf['name'][:20]:<22} "
                                 f"cc={m['courseware_id'][:12]:<12} 时长={dur:.0f}s 来源={src} "
                                 f"进度={prog.get('rate')} completed={prog.get('completed')}")
                if vids == 0:
                    lines.append("  （这门课没有视频 leaf）")
        except Exception as e:  # noqa: BLE001
            lines.append(f"自检异常：{type(e).__name__}: {e}")
        finally:
            out = BASE / "dump"
            out.mkdir(parents=True, exist_ok=True)
            text = "\n".join(lines)
            (out / "selftest.txt").write_text(text, encoding="utf-8")
            try:
                print(text)
            except Exception:  # noqa: BLE001
                pass
            app.after(200, app.destroy)

    app.after(300, run)
    app.mainloop()
    return 0

def main() -> int:
    import argparse

    paths.install_std_streams()
    ap = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    ap.add_argument("--autoload", action="store_true", help="启动后自动读取课程")
    ap.add_argument("--selftest", action="store_true", help="只读自检：cookie/章节/时长，写 dump/selftest.txt 后退出")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    app = App(auto_load=args.autoload)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
