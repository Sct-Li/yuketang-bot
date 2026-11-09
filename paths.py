# -*- coding: utf-8 -*-
"""统一路径与运行环境适配（源码运行 / PyInstaller 打包运行都能用）。

打包成 exe 后：
  · 程序自身在 sys.executable 旁边，配置文件、cookie、日志都放 exe 同目录（可写、可携带）
  · sys.stdout / sys.stderr 可能是 None（--noconsole），必须重定向到文件，否则 print 直接崩
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

APP_NAME = "雨课堂刷课助手"
APP_VERSION = "1.0"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序目录：源码运行 = 脚本目录；打包运行 = exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = app_dir()


def data_path(name: str) -> Path:
    return BASE / name


def resource_path(*parts: str) -> Path:
    """只读资源（打包后可能在 _MEIPASS 里）。"""
    root = getattr(sys, "_MEIPASS", None)
    base = Path(root) if root else BASE
    return base.joinpath(*parts)


def install_std_streams(log_dir: Optional[Path] = None) -> None:
    """windowed 模式下 stdout/stderr 为 None，重定向到日志文件，避免 print 崩溃。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = log_dir or (BASE / "dump")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = open(log_dir / "app.log", "a", encoding="utf-8", errors="replace")
    except OSError:
        fh = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh
