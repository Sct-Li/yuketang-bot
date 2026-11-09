# -*- coding: utf-8 -*-
"""课程树诊断：某门课到底该用哪个接口拉章节。

很多雨课堂账号是混合课型：
  · MOOC 型（在线课程）  -> /mooc-api/v1/lms/learn/course/chapter?cid=  有 11 章 52 知识点
  · 课堂型（校内课/直播） -> 上面那个接口返回空 data，章节在别的接口，或者根本没有 leaf
脚本对第二种只会打 "0 章 / 0 个知识点"，这个工具就是把所有候选接口挨个打一遍，
告诉你是哪一种，并把原始响落到 dump/probe/。

用法：
    python probe.py --course 形势与政策
    python probe.py --index 3
    python probe.py --classroom-id 25494916
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ykt import HOST, AuthError, Yuketang, iter_dicts

from paths import app_dir as _app_dir
BASE = _app_dir()
DUMP = BASE / "dump" / "probe"
LOG = logging.getLogger("probe")

# {cid}=classroom_id  {uv}=university_id  {uid}=user_id  {tid}=course_id(可能为空)
CANDIDATES = [
    ("mooc 章节", "/mooc-api/v1/lms/learn/course/chapter?cid={cid}"),
    ("mooc 章节+term", "/mooc-api/v1/lms/learn/course/chapter?cid={cid}&term=latest&uv_id={uv}"),
    ("v3 章节列表", "/api/v3/course/{cid}/chapter/list?classroom_id={cid}"),
    ("v3 章节列表(b)", "/api/v3/course/{cid}/chapter/list"),
    ("v3 知识点列表", "/api/v3/lesson/lesson-list?classroom_id={cid}"),
    ("v3 lesson list", "/api/v3/lesson/list?classroom_id={cid}"),
    ("v2 章节", "/v2/api/web/courses/{cid}/chapters"),
    ("v2 章节(b)", "/v2/api/web/courses/{cid}/chapter/list"),
    ("legacy 章节", "/api/classroom_course_chapter?classroom_id={cid}"),
    ("课堂课件", "/api/v3/classroom/{cid}/courseware"),
    ("课堂 on-lesson", "/api/v3/classroom/{cid}/on-lesson"),
    ("学习日志", "/v2/api/web/logs/learn/{cid}?actype=-1&page=0&offset=100&sort=-1"),
]


def summarize(data) -> str:
    """粗判这个响应里有没有"章节/知识点"结构。"""
    if not isinstance(data, (dict, list)):
        return "非 JSON"
    leaves = [d for d in iter_dicts(data) if "leaf_list" in d or "leafs" in d]
    chapter_like = [d for d in iter_dicts(data) if "section_leaf_list" in d or "course_chapter" in d]
    ids = [d for d in iter_dicts(data) if d.get("leaf_id") or d.get("id")]
    acts = [d for d in iter_dicts(data) if d.get("type") is not None and d.get("content") is not None]
    parts = []
    if chapter_like:
        parts.append(f"chapter容器={len(chapter_like)}")
    if leaves:
        parts.append(f"leaf_list={len(leaves)}")
    if ids:
        parts.append(f"带 id 节点={len(ids)}")
    if acts:
        types = sorted({d.get("type") for d in acts})
        parts.append(f"activities={len(acts)} types={types}")
    if not parts:
        keys = list(data.keys())[:8] if isinstance(data, dict) else ["<list>"]
        parts.append(f"无结构 keys={keys}")
    return "，".join(parts)


def graph_probe(client, cid: str) -> int:
    """学习空间（ai-workspace / lms-graph）专用探测。

    课程跳到 /ai-workspace/lms-graph/{cid}/video/{vid}?node_id= 时，内容在知识图谱里，
    老 leaf 接口是空的。这里把图谱链路依次打一遍，把真实字段和响应落盘。
    """
    from graphspace import API, GraphSpace

    DUMP.mkdir(parents=True, exist_ok=True)
    gs = GraphSpace(client, cid)
    LOG.info("学习空间诊断 classroom_id=%s", cid)

    steps = []
    # 1) classroom -> kg
    for name, path, method in (
        ("relate_kg(no params)", API["relate_kg"], "GET"),
        ("relate_kg+classroom", API["relate_kg"] + "?classroom_id=" + cid, "GET"),
        ("instance_list", API["instance_list"] + "?classroom_id=" + cid, "GET"),
    ):
        try:
            status, data = client.request(method, path, retries=1)
        except Exception as e:  # noqa: BLE001
            steps.append((name, path, "ERR", str(e)[:120]))
            continue
        body = data if isinstance(data, (dict, list)) else {"_raw": str(data)[:200]}
        (DUMP / f"graph_{len(steps):02d}_{name.replace(' ', '_').replace('/', '_')}.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2)[:200000], encoding="utf-8")
        steps.append((name, path, status, summarize(body)))
        LOG.info("%-22s %-70s %s  %s", name, path[:70], status, summarize(body))

    kg = gs.resolve()
    if not kg:
        print("\n没识别到 graph_id。把 dump/probe/graph_*.json 发出来即可定位。")
        return 0
    print(f"\n✔ graph_id = {kg}")

    # 2) 节点
    nodes = gs.nodes(force=True)
    print(f"✔ nodes = {len(nodes)}，前 3 个：")
    for n in nodes[:3]:
        print(f"   node_id={n['node_id']} name={n['name']} video_id={n.get('video_id')} leaf_id={n.get('leaf_id')}")

    # 3) 单节点详情 + 视频信息
    if nodes:
        node = nodes[0]
        for name, path in (
            ("node_detail_stu", API["node_detail_stu"].format(kg=kg, node=node["node_id"], cid=cid)),
            ("relate_classroom_leaf", API["relate_classroom_leaf"].format(kg=kg, node=node["node_id"])),
        ):
            try:
                status, data = client.request("GET", path, retries=1)
            except Exception as e:  # noqa: BLE001
                print(f"   {name}: ERR {e}")
                continue
            body = data if isinstance(data, (dict, list)) else {"_raw": str(data)[:200]}
            (DUMP / f"graph_node_{name}.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=2)[:200000], encoding="utf-8")
            print(f"   {name}: {status} {summarize(body)}")
        if node.get("video_id"):
            detail = gs.video_detail(node["video_id"], node["node_id"])
            print(f"   video_info: {json.dumps(detail, ensure_ascii=False)[:220]}")

    # 4) 学习情况 / 最近节点
    sit = gs.study_situation()
    print(f"✔ study_situation: {json.dumps(sit, ensure_ascii=False)[:260]}")
    latest = gs.latest_node()
    print(f"✔ latest_study_node: {json.dumps(latest, ensure_ascii=False)[:260]}")

    # 5) 试一次标记（只打一个节点，验证接口是否接受 body）
    if nodes:
        node = nodes[0]
        res = gs.mark_studied(node["node_id"], node.get("leaf_id"))
        print(f"\n标记测试 node={node['node_id']}: ok={res['ok']} how={res['how']}")
        print(f"响应: {json.dumps(res.get('resp'), ensure_ascii=False)[:300]}")
    print("\n原始响应已落 dump/probe/。把这些 json 发出来就能确定字段。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="课程树诊断")
    ap.add_argument("--config", default=str(BASE / "config.json"))
    ap.add_argument("--cookies", default="")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--course", help="课程名关键词")
    ap.add_argument("--index", type=int, help="课程编号（--list 里的 #）")
    ap.add_argument("--classroom-id", dest="classroom_id", help="直接给 classroom_id")
    ap.add_argument("--graph", action="store_true", help="只诊断学习空间（ai-workspace/lms-graph）链路")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    cfg = {}
    p = Path(args.config)
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8-sig"))
        except ValueError:
            pass

    # 复用 main.py 的 cookie 解析 + 自动捕捉
    import main as cli
    ns = argparse.Namespace(cookies=args.cookies or cfg.get("cookie_string", ""), csrftoken=None,
                            sessionid=None, uv_id=None, config=args.config, auto_cookie=False,
                            no_capture=False, capture_mode="auto", capture_port=9222, wait=300)
    csrf, sid, uv = cli.resolve_auth(ns, cfg)
    if not csrf or not sid:
        csrf, sid, uv, ok = cli.auto_capture(ns, cfg)
        if not ok:
            LOG.error("没有 cookie 且自动捕捉失败")
            return 2

    client = Yuketang(csrf, sid, uv, host=args.host, profile_path=BASE / "profile.json")
    client.detect()

    if args.classroom_id:
        cid = str(args.classroom_id)
        name = f"classroom {cid}"
        picked = {"classroom_id": cid, "name": name}
    else:
        courses = client.list_courses()
        if not courses:
            LOG.error("课程列表为空")
            return 4
        picked = None
        if args.index:
            picked = courses[args.index - 1] if 1 <= args.index <= len(courses) else None
        elif args.course:
            hits = [c for c in courses if args.course in (c.get("name") or "") or args.course in (c.get("full_name") or "")]
            picked = hits[0] if len(hits) == 1 else None
            if len(hits) > 1:
                LOG.error("匹配到多门：%s", "、".join(h["name"] for h in hits[:6]))
        if picked is None:
            for i, c in enumerate(courses, 1):
                LOG.info("%2d  %-28s classroom_id=%s", i, c["name"], c["classroom_id"])
            LOG.error("用 --index 或 --course 指定一门")
            return 5

    cid = str(picked["classroom_id"])
    client.set_course(cid)
    LOG.info("诊断《%s》 classroom_id=%s user_id=%s uv_id=%s",
             picked.get("name"), cid, client.user_id, client.uv_id)

    if args.graph:
        return graph_probe(client, cid)

    DUMP.mkdir(parents=True, exist_ok=True)
    results = []
    for i, (label, tpl) in enumerate(CANDIDATES):
        path = tpl.format(cid=cid, uv=client.uv_id or 0, uid=client.user_id or 0, tid=client.course_id or 0)
        try:
            status, data = client.request("GET", path, retries=1)
        except AuthError:
            LOG.error("cookie 失效")
            return 3
        except Exception as e:  # noqa: BLE001
            LOG.info("%-14s %-64s 异常 %s", label, path[:64], e)
            results.append((label, path, "ERR", str(e)))
            continue
        body = data if isinstance(data, (dict, list)) else {"_raw": str(data)[:200]}
        (DUMP / f"{cid}_{i:02d}_{label.replace(' ', '_')}.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2)[:200000], encoding="utf-8")
        summary = summarize(body)
        results.append((label, path, status, summary))
        LOG.info("%-14s %-64s %s  %s", label, path[:64], status, summary)

    # 结论
    good = [r for r in results if r[2] == 200 and ("leaf" in r[3] or "chapter" in r[3])]
    print()
    if good:
        print("可用路线：")
        for label, path, status, summary in good:
            print(f"  ✔ {label}: {path}\n    {summary}")
        print("\n把上面这条路径发我，或者直接改 ykt.py 的 CANDIDATES['chapters'] 首项即可。")
    else:
        print("没有接口返回章节结构 —— 这门课大概率是「课堂型」（直播/录播课），")
        print("学习内容不在 leaf 章节里，脚本本身就无需刷；原始响应已存 dump/probe/。")
        for label, path, status, summary in results:
            if status == 200:
                print(f"  · {label}: {summary}")

    # 0 章课程再看一眼活动日志里到底是什么，以及活动里的 leaf_id 能不能查详情
    log_json = None
    for f in sorted(DUMP.glob(f"{cid}_*学习日志*.json")):
        try:
            log_json = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            pass
    acts = []
    if isinstance(log_json, dict):
        acts = ((log_json.get("data") or {}).get("activities") or [])
    if acts:
        print(f"\n活动日志 activities {len(acts)} 条，前 3 条原样：")
        for a in acts[:3]:
            print("  ", json.dumps(a, ensure_ascii=False)[:280])
        leaf_ids = [str((a.get("content") or {}).get("leaf_id")) for a in acts
                    if (a.get("content") or {}).get("leaf_id")]
        if leaf_ids:
            print("\n用活动里的 leaf_id 试 leaf_info（能查到就说明可以按 leaf 刷）：")
            for lid in leaf_ids[:3]:
                try:
                    info = client.leaf_info(lid)
                except Exception as e:  # noqa: BLE001
                    print(f"   leaf {lid}: 异常 {e}")
                    continue
                print(f"   leaf {lid}: {('有数据 ' + str(list(info.keys())[:6])) if info else '空'}")
        types = sorted({a.get("type") for a in acts})
        print(f"\nactivities 的 type 分布：{types}（type=17 是课件/视频类）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
