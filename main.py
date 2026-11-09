# -*- coding: utf-8 -*-
"""长江雨课堂 自动识别 + 自动学习 —— CLI。

    python main.py --auto --cookies "csrftoken=..; sessionid=.."       # 识别 + 全自动
    python main.py --list --cookies "..."                              # 只看识别结果和课程
    python main.py --course 全球化 --cookies "..."                      # 只跑匹配的课
    python main.py --plan --course 全球化 --cookies "..."               # 打印课程树与类型，不上报
    python main.py --auto --realtime --cookies "..."                    # 真速（心跳按墙钟节奏）
    python main.py --auto --redetect --cookies "..."                    # 强制重新识别接口

识别产物 profile.json：host / uv_id / user_id / endpoints / leaf_kinds。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ykt import HOST, AuthError, Yuketang
from player import run_course

from paths import app_dir as _app_dir
BASE = _app_dir()
LOG = logging.getLogger("ykt")


# ---------------------------------------------------------------------------
def parse_cookie_string(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    raw = (raw or "").strip()
    if "\t" in raw:  # Netscape cookies.txt
        for line in raw.splitlines():
            parts = line.strip().split("\t")
            if len(parts) >= 7 and not line.startswith("#"):
                out[parts[5]] = parts[6]
        return out
    for chunk in raw.replace("\n", ";").split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def resolve_auth(args, cfg: Dict[str, Any]) -> tuple:
    cookies: Dict[str, str] = {}
    raw = args.cookies or cfg.get("cookie_string") or ""
    if raw.startswith("@"):  # --cookies @cookie.txt
        p = Path(raw[1:])
        if not p.is_absolute():
            p = BASE / p
        raw = p.read_text(encoding="utf-8-sig") if p.exists() else ""
    if raw:
        cookies = parse_cookie_string(raw)
    cookies.update({k: v for k, v in (cfg.get("cookies") or {}).items() if v})
    csrf = args.csrftoken or cookies.get("csrftoken") or os.environ.get("YKT_CSRFTOKEN", "")
    sid = args.sessionid or cookies.get("sessionid") or os.environ.get("YKT_SESSIONID", "")
    uv = args.uv_id or cookies.get("university_id") or cfg.get("uv_id") or os.environ.get("YKT_UV_ID", "")
    return csrf, sid, uv


def print_courses(courses: List[Dict[str, Any]]) -> None:
    print(f"{'#':<4}{'课程':<34}{'classroom_id':<15}{'学期':<14}角色")
    print("-" * 82)
    for i, c in enumerate(courses, 1):
        print(f"{i:<4}{str(c['name'])[:32]:<34}{c['classroom_id']:<15}{str(c['term'])[:12]:<14}{c['role']}")


def print_plan(client: Yuketang, course: Dict[str, Any]) -> None:
    client.set_course(course["classroom_id"])
    chapters = client.chapters()
    leaves = client.iter_leaves(chapters)
    print(f"\n《{course['name']}》 classroom_id={course['classroom_id']}  "
          f"{len(chapters)} 章 / {len(leaves)} 个知识点")
    print(f"{'章节':<20}{'小节':<26}{'leaf_type':>9}{'类型':<8}{'时长s':>8}{'页数':>5}{'完成':>6}  名称")
    print("-" * 110)
    for leaf in leaves:
        info = client.leaf_info(leaf["leaf_id"])
        meta = client.classify(leaf, info)
        if meta["kind"] == "video":
            client.course_id = meta["course_id"] or client.course_id
            client.sku_id = meta["sku_id"] or client.sku_id
            prog = client.progress(leaf["leaf_id"])
            done = "✓" if client.is_completed(prog) else f"{client.rate(prog)*100:.0f}%"
        else:
            done = "-"
        print(f"{str(leaf['chapter'])[:18]:<20}{str(leaf['section'])[:24]:<26}"
              f"{str(leaf['leaf_type']):>9}{meta['kind']:<8}{meta['duration']:>8.0f}"
              f"{meta['slides']:>5}{done:>6}  {leaf['name'][:26]}")


def auto_capture(args, cfg: Dict[str, Any]) -> tuple:
    """没有可用 cookie 时自动捕捉：本地库 -> 浏览器调试端口（等你登录）。"""
    try:
        import capture as cap
    except Exception as e:  # noqa: BLE001
        LOG.warning("capture.py 不可用：%s", e)
        return None, "", "", False
    LOG.info("没有可用 cookie，自动捕捉（mode=%s）…", args.capture_mode)
    jar = cap.capture(mode=args.capture_mode, port=args.capture_port, wait=args.wait)
    if not jar:
        return None, "", "", False
    cap.save_cookies(jar)
    new_cfg: Dict[str, Any] = {}
    try:
        new_cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        pass
    csrf, sid, uv = resolve_auth(args, new_cfg or cfg)
    return csrf, sid, uv, bool(csrf and sid)


def pick_course(courses: List[Dict[str, Any]], keyword: Optional[str], index: Optional[int]) -> Optional[Dict[str, Any]]:
    if index:
        if 1 <= index <= len(courses):
            return courses[index - 1]
        LOG.error("编号 %s 超出范围 1-%d", index, len(courses))
        return None
    if keyword:
        hits = [c for c in courses if keyword in (c.get("name") or "") or keyword in (c.get("full_name") or "")]
        if len(hits) == 1:
            return hits[0]
        LOG.error("关键词「%s」匹配 %d 门，请用 --index 指定：%s", keyword, len(hits),
                  "、".join(c["name"] for c in hits[:8]))
        return None
    return None


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="长江雨课堂 自动识别 + 自动学习")
    ap.add_argument("--config", default=str(BASE / "config.json"))
    ap.add_argument("--profile", default=str(BASE / "profile.json"))
    ap.add_argument("--cookies", help='整串 "csrftoken=..; sessionid=..; university_id=2803"')
    ap.add_argument("--csrftoken")
    ap.add_argument("--sessionid")
    ap.add_argument("--uv-id", dest="uv_id")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--redetect", action="store_true", help="忽略 profile.json，重新识别接口")
    ap.add_argument("--list", action="store_true", help="只识别 + 列课程")
    ap.add_argument("--auto", action="store_true", help="识别后自动跑所有课程的未完成视频")
    ap.add_argument("--plan", action="store_true", help="打印课程树/类型/进度，不上报")
    ap.add_argument("--course", help="课程名关键词")
    ap.add_argument("--index", type=int, help="按 --list 编号选课")
    ap.add_argument("--only-video", dest="only_video", action="store_true", default=True)
    ap.add_argument("--with-slide", dest="with_slide", action="store_true", help="同时跑课件(幻灯片)心跳")
    ap.add_argument("--step", type=int, default=20, help="每条心跳覆盖秒数（官方约 1s，批量补课默认 20）")
    ap.add_argument("--batch", type=int, default=50, help="单次心跳条数上限（官方 localStorage 上限 50）")
    ap.add_argument("--realtime", action="store_true", help="按墙钟真速推进（更稳，更慢）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少个知识点")
    ap.add_argument("--no-activities-fallback", dest="activities_fallback", action="store_false",
                    help="无 leaf 章节时不用活动日志兜底（默认用）")
    ap.add_argument("--no-graph-fallback", dest="graph_fallback", action="store_false",
                    help="无 leaf 章节时不用学习空间(lms-graph)兜底（默认用）")
    ap.add_argument("--gap", default="0.2,0.6", help="知识点之间休息秒区间 a,b")
    ap.add_argument("--auto-cookie", dest="auto_cookie", action="store_true",
                    help="强制先自动捕捉 cookie（等同 --cookies auto）")
    ap.add_argument("--relogin", action="store_true",
                    help="强制重新抓 cookie（sessionid 被踢/过期时用）")
    ap.add_argument("--no-capture", dest="no_capture", action="store_true",
                    help="cookie 缺失/失效时不自动捕捉")
    ap.add_argument("--capture-mode", dest="capture_mode", default="auto",
                    choices=["auto", "cdp", "db"], help="捕捉方式：auto/cdp/db")
    ap.add_argument("--capture-port", dest="capture_port", type=int, default=9222)
    ap.add_argument("--wait", type=float, default=300, help="等你在浏览器里登录的秒数")
    ap.add_argument("--dump", action="store_true", help="把识别到的原始响应写 dump/")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg: Dict[str, Any] = {}
    if Path(args.config).exists():
        try:
            cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        except ValueError:
            LOG.warning("config.json 不是合法 JSON，忽略")

    if args.cookies and args.cookies.strip().lower() == "auto":
        args.cookies = ""          # --cookies auto 等价于 --auto-cookie
        args.auto_cookie = True
        args.no_capture = False

    captured = False
    if args.relogin:
        args.auto_cookie = True
    if args.auto_cookie:
        csrf, sid, uv, captured = auto_capture(args, cfg)
    else:
        csrf, sid, uv = resolve_auth(args, cfg)
        if (not csrf or not sid) and not args.no_capture:
            csrf, sid, uv, captured = auto_capture(args, cfg)
    if not csrf or not sid:
        LOG.error("缺 csrftoken / sessionid。三条路：① 跑 python capture.py 自动抓；"
                  "② --cookies \"csrftoken=..; sessionid=..\"；③ 粘进 config.json 的 cookie_string")
        return 2

    client = Yuketang(csrf, sid, uv, host=args.host, profile_path=Path(args.profile))

    try:
        client.detect(redetect=args.redetect)
        courses = client.list_courses()
    except AuthError as e:
        if args.no_capture:
            LOG.error("认证失败：%s", e)
            return 3
        LOG.warning("登录态失效（%s），自动重新捕捉 cookie", e)
        csrf, sid, uv, ok = auto_capture(args, cfg)
        if not ok:
            return 3
        client = Yuketang(csrf, sid, uv, host=args.host, profile_path=Path(args.profile))
        try:
            client.detect(redetect=args.redetect)
            courses = client.list_courses()
        except Exception as e2:  # noqa: BLE001
            LOG.error("重新认证后仍然失败：%s", e2)
            return 3
    except Exception as e:  # noqa: BLE001
        LOG.error("识别失败：%s", e)
        return 4

    if not courses:
        LOG.error("课程列表为空：确认账号已加入课程，或 --redetect 重新识别")
        return 4

    print_courses(courses)
    LOG.info("识别到的接口：%s", json.dumps(client.endpoints, ensure_ascii=False))

    if args.dump:
        dump = BASE / "dump"
        dump.mkdir(exist_ok=True)
        (dump / "courses.json").write_text(json.dumps(courses, ensure_ascii=False, indent=2), encoding="utf-8")
        (dump / "profile.json").write_text(json.dumps(client.profile, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("原始数据已落 dump/")

    if args.list:
        return 0

    targets: List[Dict[str, Any]] = []
    picked = pick_course(courses, args.course, args.index)
    if picked:
        targets = [picked]
    elif args.auto or args.plan:
        targets = courses
    else:
        LOG.error("没选课：用 --course 关键词、--index 编号，或 --auto 跑全部")
        return 5

    if args.plan:
        for c in targets:
            print_plan(client, c)
        return 0

    lo, hi = (float(x) for x in args.gap.split(","))
    total = {"done": 0, "skipped": 0, "failed": 0}
    for course in targets:
        LOG.info("=== 开始《%s》（classroom_id=%s）===", course["name"], course["classroom_id"])
        try:
            stats = run_course(
                client, course,
                with_slide=args.with_slide,
                step=args.step,
                batch_size=args.batch,
                realtime=args.realtime,
                gap=(lo, hi),
                limit=args.limit,
                activities_fallback=args.activities_fallback,
                graph_fallback=args.graph_fallback,
            )
        except AuthError as e:
            if args.no_capture:
                LOG.error("登录态失效：%s", e)
                return 3
            LOG.warning("跑到一半登录态失效（%s），自动重新捕捉 cookie 后继续", e)
            nc, ns, nu, ok = auto_capture(args, cfg)
            if not ok:
                return 3
            client.csrftoken, client.sessionid = nc, ns
            if nu:
                client.uv_id = nu
            continue
        except Exception as e:  # noqa: BLE001
            LOG.exception("课程处理异常：%s", e)
            continue
        for k in total:
            total[k] += stats.get(k, 0)
        LOG.info("《%s》结果：%s", course["name"], stats)

    LOG.info("全部结束：完成 %d，已跳过 %d，失败 %d", total["done"], total["skipped"], total["failed"])
    return 0 if total["failed"] == 0 else 6


if __name__ == "__main__":
    sys.exit(main())
