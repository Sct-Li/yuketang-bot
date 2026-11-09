# -*- coding: utf-8 -*-
"""打包成傻瓜式 exe（PyInstaller）。

    python build.py            # 生成 dist/雨课堂刷课助手.exe + dist/使用说明.txt
    python build.py --clean    # 先清掉 build/dist 再打

产物是单文件窗口程序，双击即用：配置、cookie、日志都写在 exe 同目录。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ASSETS = ROOT / "assets"
EXE_NAME = "雨课堂刷课助手"

VERSION_INFO = """
VSVersionInfo(
  ffi=FixedFileInfo(filevers=(1, 0, 0, 0), prodvers=(1, 0, 0, 0),
                    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
        StringStruct('CompanyName', 'ykt-tools'),
        StringStruct('FileDescription', '雨课堂刷课助手'),
        StringStruct('FileVersion', '1.0.0.0'),
        StringStruct('InternalName', 'yuketang-helper'),
        StringStruct('OriginalFilename', '雨课堂刷课助手.exe'),
        StringStruct('ProductName', '雨课堂刷课助手'),
        StringStruct('ProductVersion', '1.0.0.0')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""

README_TXT = """雨课堂刷课助手 v1.0 — 使用说明
=====================================

一、第一次使用（三步）
1. 双击「雨课堂刷课助手.exe」
2. 点「① 自动抓 Cookie」：程序会拉起一个浏览器窗口，在里面登录雨课堂
   （登录一次即可，之后这个专用配置会记住登录态，再抓就是几秒）
3. 点「② 读取课程」→ 表格里勾选课程 → 点「③ 开始刷课」

二、界面速查
· 表格列：课程 / classroom_id / 章 / 知识点 / 视频 / 已完成 / 类型 / 总时长 / 状态
    状态「可刷」= 有未完成视频；「已完成」= 全看完；「无内容」= 课堂型/无可用课件
· 类型：在线课（普通章节）/ 学习空间（ai-workspace 课程）/ 活动兜底
· 双击某行 = 在浏览器打开该课程页；右键 = 只刷这门课 / 复制 classroom_id
· 快捷键：F5 读取课程、F9 开始刷课、Esc 停止、Ctrl+A 全选
· 参数：心跳秒(20) / 批大小(50) / 每课上限(0=不限) / 真速推进 / 含课件幻灯片
    真速推进更稳（墙钟对齐）；关掉会快，但风控视角不自然

三、文件说明（都在 exe 同目录）
· config.json     cookie 与参数（程序会自动写，别删）
· cookie.txt      最近一次抓到的 cookie
· profile.json    自动识别出的接口，删了会重新识别
· dump\\           日志、抓包、错误现场

四、常见问题
· 提示「缺 cookie」：点「① 自动抓 Cookie」，或在浏览器登录后让它自己抓
· 提示 401：程序会自动重抓；若失败，关掉浏览器再点「① 自动抓 Cookie」
· 提示「调试端口没开」：多半是上次的浏览器没退干净，程序会自动清理；仍不行就
  关掉所有 Edge 窗口后重试
· 课程显示「无内容」：该课程是课堂型（直播/实习），没有可刷的视频
· 想确认某门课的真实接口：命令行运行同目录下的 probe.py（源码包内提供）

五、注意
· 单账号串行跑，别多开；一天刷几门就够，别挂整夜
· 考试/测验期间别刷，避免时间戳冲突
"""


def make_icon() -> Path:
    ASSETS.mkdir(exist_ok=True)
    ico = ASSETS / "app.ico"
    if ico.exists():
        return ico
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return ico if ico.exists() else Path()
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, size - 8, size - 8], radius=52, fill=(37, 99, 235, 255))
    # 播放三角
    d.polygon([(100, 78), (100, 178), (182, 128)], fill=(255, 255, 255, 255))
    # 底部进度条
    d.rounded_rectangle([64, 196, 192, 210], radius=7, fill=(219, 234, 254, 255))
    d.rounded_rectangle([64, 196, 150, 210], radius=7, fill=(74, 222, 128, 255))
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ico


def run(cmd: list) -> int:
    print(">>", " ".join(str(c) for c in cmd))
    return subprocess.call([str(c) for c in cmd])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--onefile", action="store_true",
                    help="单文件版（体积小但启动要解压到 TEMP，部分杀软会拦）")
    ap.add_argument("--zip", action="store_true", help="额外打一个 zip 发布包")
    args = ap.parse_args()

    if args.clean:
        for p in (BUILD, DIST, ROOT / f"{EXE_NAME}.spec", ROOT / "yuketang.spec"):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()

    icon = make_icon()
    vi = BUILD / "version_info.txt"
    BUILD.mkdir(parents=True, exist_ok=True)
    vi.write_text(VERSION_INFO, encoding="utf-8")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if args.onefile else "--onedir",
        "--windowed",
        "--name", EXE_NAME,
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--version-file", str(vi),
        "--add-data", f"{ROOT / 'tools'};tools",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "videometa",
        "--hidden-import", "graphspace",
    ]
    if icon.exists():
        cmd += ["--icon", str(icon)]
    cmd.append(str(ROOT / "gui.py"))

    rc = run(cmd)
    if rc != 0:
        print("打包失败，退出码", rc)
        return rc

    if args.onefile:
        target_dir = DIST
        exe = DIST / f"{EXE_NAME}.exe"
    else:
        target_dir = DIST / EXE_NAME
        exe = target_dir / f"{EXE_NAME}.exe"
    (target_dir / "使用说明.txt").write_text(README_TXT, encoding="utf-8")

    if exe.exists():
        mb = exe.stat().st_size / 1024 / 1024
        print(f"\n完成：{exe}（exe {mb:.1f} MB）")
        print(f"说明：{target_dir / '使用说明.txt'}")

    if args.zip and not args.onefile:
        archive = DIST / f"{EXE_NAME}-v1.0-win64"
        shutil.make_archive(str(archive), "zip", root_dir=str(DIST), base_dir=EXE_NAME)
        print(f"压缩包：{archive}.zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
