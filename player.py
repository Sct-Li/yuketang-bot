# -*- coding: utf-8 -*-
"""播放 / 上报调度层。

视频：按官方播放器 player.newHeartlog() 的字段批量构造心跳，一批 50 条、每条 +20s，
      发完立刻轮询 /video-log/get_video_watch_progress/ 看 rate 是否前进、completed 是否置 1。
课件：leaf_info 里有 cards 的走幻灯片心跳（t=slide, cards_id），接口行为不确定，
      默认不跑，需要 `--with-slide` 显式开。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from ykt import Yuketang, TransientError, AuthError, deep_first

LOG = logging.getLogger("ykt.player")

# 每批发完后的确认间隔：越短越灵敏；原来固定 8×1.2s，单批最坏等 14 秒
CONFIRM_DELAYS = (0.25, 0.5, 1.0)


# ---------------------------------------------------------------------------
# 时长
# ---------------------------------------------------------------------------
def resolve_duration(client: Yuketang, leaf: Dict[str, Any], duration: float,
                     siblings: List[Tuple[str, float]],
                     prog: Optional[Dict[str, Any]] = None) -> Tuple[float, str]:
    """确定视频时长，按可靠性排序返回 (秒, 来源)。

    1. leaf_info 的 media.duration
    2. 进度接口的 video_length（看过才有）
    3. 播放地址的 mp4 moov->mvhd（videometa，未看过也能拿到）
    4. 同章其它视频的最大值（最后兜底，宁可估也不卡住）
    """
    if duration > 0:
        client._durations[leaf["leaf_id"]] = duration
        return duration, "leaf_info"

    if prog:
        try:
            server_len = float(prog.get("video_length") or 0)
        except (TypeError, ValueError):
            server_len = 0.0
        if server_len > 0:
            client._durations[leaf["leaf_id"]] = server_len
            LOG.info("leaf %s 用进度接口 video_length=%.0fs", leaf["leaf_id"], server_len)
            return server_len, "progress"

    cc = leaf.get("courseware_id") or leaf.get("ccid")
    if cc:
        try:
            from videometa import duration_from_playurl
            meta_dur = duration_from_playurl(client, str(cc))
            if meta_dur and meta_dur > 0:
                client._durations[leaf["leaf_id"]] = meta_dur
                LOG.info("leaf %s 从播放地址解析出时长 %.0fs（未看过也能拿）",
                         leaf["leaf_id"], meta_dur)
                return meta_dur, "mp4"
        except Exception as e:  # noqa: BLE001
            LOG.debug("videometa 失败：%s", e)

    cands = [d for lid, d in siblings if lid != leaf["leaf_id"] and d > 0]
    if not cands:
        cands = [d for d in client._durations.values() if d > 0]
    if cands:
        est = max(cands)
        LOG.warning("leaf %s 时长未知，按同章最大 %.0fs 估算", leaf["leaf_id"], est)
        client._durations[leaf["leaf_id"]] = est
        return est, "sibling"
    return 0.0, "unknown"


# ---------------------------------------------------------------------------
# 视频
# ---------------------------------------------------------------------------
def watch_video(
    client: Yuketang,
    leaf: Dict[str, Any],
    *,
    duration: float,
    step: int = 20,
    batch_size: int = 50,
    realtime: bool = False,
    poll_rounds: int = 8,
    poll_interval: float = 1.2,
    stop_event=None,
    siblings: Optional[List[Tuple[str, float]]] = None,
    on_beat=None,
) -> str:
    """把单个视频推到 completed=1。返回 done / skipped / failed。"""
    leaf_id = leaf["leaf_id"]
    title = f"{leaf['chapter']} / {leaf['section']}" if leaf["chapter"] else leaf["name"]
    prog = client.progress(leaf_id)
    if client.is_completed(prog):
        LOG.info("《%s》已完成 %.0f%%，跳过", title, client.rate(prog) * 100)
        return "skipped"

    courseware_id = client._courseware.get(leaf_id) or leaf.get("courseware_id") or leaf_id
    # 时长三级兜底：leaf_info → 进度 video_length → mp4 moov（未看过也能拿）→ 同章估算
    target, source = resolve_duration(client, leaf, duration, siblings or [], prog)
    if target <= 0:
        LOG.warning("《%s》四路都拿不到时长（leaf_info/进度/mp4/同章），跳过", title)
        return "failed"
    if source != "leaf_info":
        LOG.info("《%s》时长 %.0fs（来源 %s）", title, target, source)

    try:
        client.mark_study_leaf(leaf_id)
    except AuthError:
        raise
    except Exception:  # noqa: BLE001
        pass

    LOG.info("《%s》开始：时长 %.0fs，当前 %.0f%%", title, target, client.rate(prog) * 100)
    pg = f"{leaf_id}_{random.randint(10000, 99999)}"
    ts_base = int(time.time() * 1000)
    sq = 1
    cp = 0.0
    stagnant = 0
    wall_start = time.time()
    deadline = wall_start + max(180.0, (target / max(step, 1)) * (1.9 if realtime else 1.1))

    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            LOG.warning("《%s》收到停止信号，中断", title)
            return "stopped"
        entries: List[Dict[str, Any]] = []
        while cp < target and len(entries) < batch_size:
            cp = min(cp + step, target)
            entries.append(client.heartbeat_entry(
                et="videoend" if cp >= target else "loadeddata",
                cp=cp, sq=sq, ts_ms=ts_base + sq * 300, pg=pg,
                leaf_id=leaf_id, courseware_id=courseware_id,
                api_user_id=client.user_id, duration=target,
            ))
            sq += 1

        if realtime:
            # 让墙钟跟上报时长对齐（每批 step*batch_size 秒）
            want = wall_start + cp * 1.02
            drift = want - time.time()
            if drift > 0:
                time.sleep(min(drift, 120))

        if entries and not client.send_heartbeat(entries):
            time.sleep(0.6)
            if not client.send_heartbeat(entries):
                LOG.warning("《%s》心跳两次失败", title)
                return "failed"

        # 快速确认：只查 1~3 次、间隔 0.25/0.5/1.0 秒；进度一前进就发下一批
        advanced = False
        cur = prog
        for delay in CONFIRM_DELAYS:
            time.sleep(delay)
            cur = client.progress(leaf_id)
            if client.is_completed(cur):
                LOG.info("《%s》完成 ✅ %.0f%%（%.0fs/%.0fs）", title,
                         client.rate(cur) * 100, float(cur.get("watch_length") or 0), target)
                return "done"
            if client.rate(cur) > client.rate(prog):
                advanced = True
                break
        if advanced:
            stagnant = 0
            prog = cur
            if on_beat is not None:
                try:
                    on_beat(int(cp), int(target))
                except Exception:  # noqa: BLE001
                    pass
        else:
            stagnant += 1
            LOG.info("《%s》%.0f%%（已看 %.0fs/%.0fs, 第 %d 次未确认）", title,
                     client.rate(prog) * 100, float(prog.get("watch_length") or 0),
                     target, stagnant)

        try:
            server_len = float((cur or prog).get("video_length") or 0)
        except (TypeError, ValueError):
            server_len = 0.0
        if server_len > target:
            LOG.info("《%s》服务端时长 %.0fs > 元数据 %.0fs，补齐", title, server_len, target)
            target = server_len
        if not advanced and stagnant >= 2:
            LOG.warning("《%s》连续两批没被服务端接受，停止避免空转", title)
            break
        if cp >= target:
            break

    cur = client.progress(leaf_id)
    if client.is_completed(cur):
        LOG.info("《%s》完成 ✅ %.0f%%", title, client.rate(cur) * 100)
        return "done"
    LOG.error("《%s》未完成（%.0f%%）", title, client.rate(cur) * 100)
    return "failed"


# ---------------------------------------------------------------------------
# 课件（幻灯片）
# ---------------------------------------------------------------------------
def watch_slide(client: Yuketang, leaf: Dict[str, Any], *, slides: int,
                delay: Tuple[float, float] = (1.2, 3.0)) -> str:
    """按卡片逐页打 slide 心跳。接口行为未完全确认，作为 best-effort。"""
    if slides <= 0:
        return "failed"
    leaf_id = leaf["leaf_id"]
    title = f"{leaf['chapter']} / {leaf['section']}" or leaf["name"]
    ts_base = int(time.time() * 1000)
    pg = f"{leaf_id}_{random.randint(10000, 99999)}"
    LOG.info("《%s》课件 %d 页，逐页上报", title, slides)
    for i in range(slides):
        entry = client.heartbeat_entry(
            et="videoend" if i + 1 >= slides else "loadeddata",
            cp=float(i + 1), sq=i + 1, ts_ms=ts_base + (i + 1) * 900, pg=pg,
            leaf_id=leaf_id, courseware_id=client._courseware.get(leaf_id) or leaf_id,
            api_user_id=client.user_id, duration=float(slides),
            t="slide", slide=i + 1, cards_id=i + 1,
        )
        if not client.send_heartbeat([entry]):
            LOG.warning("《%s》第 %d 页上报失败", title, i + 1)
        time.sleep(random.uniform(*delay))
    LOG.info("《%s》课件上报结束（以服务端进度为准）", title)
    return "done"


# ---------------------------------------------------------------------------
# 整门课
# ---------------------------------------------------------------------------
def run_course(
    client: Yuketang,
    course: Dict[str, Any],
    *,
    only_video: bool = True,
    with_slide: bool = False,
    step: int = 20,
    batch_size: int = 50,
    realtime: bool = False,
    gap: Tuple[float, float] = (0.2, 0.6),
    limit: int = 0,
    activities_fallback: bool = True,
    graph_fallback: bool = True,
    stop_event=None,
    on_leaf=None,
    on_beat=None,
) -> Dict[str, int]:
    client.set_course(course["classroom_id"])
    try:
        chapters = client.chapters()
    except AuthError:
        raise
    except Exception as e:  # noqa: BLE001
        LOG.info("  章节接口报错（%s），继续试学习空间/活动日志兜底", str(e)[:80])
        chapters = []
    leaves = client.iter_leaves(chapters)
    LOG.info("《%s》：%d 章 / %d 个知识点", course["name"], len(chapters), len(leaves))
    if not leaves and activities_fallback:
        alt = client.activity_leaves()
        if alt:
            leaves = alt
            LOG.info("  ↳ 无 leaf 章节，用活动日志兜底构造 %d 个知识点（实验性，以服务端进度为准）", len(alt))
    if not leaves and graph_fallback:
        # 雨课堂升级后：课程跳到「学习空间」(ai-workspace/lms-graph)，内容在知识图谱里
        try:
            from graphspace import GraphSpace, watch_graph_videos
            gs = GraphSpace(client, course["classroom_id"])
            if gs.resolve() and gs.videos():
                LOG.info("  ↳ 这门课是「学习空间」课（kg=%s），按图谱视频刷", gs.kg)
                return watch_graph_videos(client, gs, step=step, batch_size=batch_size,
                                          realtime=realtime, stop_event=stop_event,
                                          on_leaf=on_leaf)
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            LOG.debug("学习空间兜底失败：%s", e)
    if not leaves:
        acts = 0
        try:
            acts = len(client.courseware_map())
        except Exception:  # noqa: BLE001
            pass
        LOG.info("  ↳ 这门课是「课堂型」（直播/录播），没有 leaf 章节，脚本跳过；活动日志里 %d 条课件记录", acts)
        LOG.info("  ↳ 想确认该用哪个接口：python probe.py --classroom-id %s", course["classroom_id"])
        return {"done": 0, "skipped": 0, "failed": 0}

    client.courseware_map()

    # 先把所有视频的时长摸出来，供 duration=0 的兄弟估算
    info_cache: Dict[str, Dict[str, Any]] = {}
    for leaf in leaves:
        info = client.leaf_info(leaf["leaf_id"])
        info_cache[leaf["leaf_id"]] = info
        meta = client.classify(leaf, info)
        leaf.update(meta)
        if meta["kind"] == "video":
            client.course_id = meta["course_id"] or client.course_id
            client.sku_id = meta["sku_id"] or client.sku_id
    LOG.info("类型分布：%s", _kind_stats(leaves))

    siblings = [(l["leaf_id"], float(l.get("duration") or 0)) for l in leaves if l.get("kind") == "video"]

    stats = {"done": 0, "skipped": 0, "failed": 0}
    todo = [l for l in leaves if l.get("kind") in ("video", "slide")]
    if only_video and not with_slide:
        todo = [l for l in todo if l["kind"] == "video"]
    if limit:
        todo = todo[:limit]

    for idx, leaf in enumerate(todo, 1):
        if stop_event is not None and stop_event.is_set():
            LOG.warning("收到停止信号，剩余 %d 个知识点不再处理", len(todo) - idx + 1)
            stats["stopped"] = len(todo) - idx + 1
            break
        if leaf.get("locked"):
            LOG.warning("[%d/%d]《%s》未解锁，跳过", idx, len(todo), leaf["name"])
            stats["failed"] += 1
            continue
        info = info_cache.get(leaf["leaf_id"]) or client.leaf_info(leaf["leaf_id"])
        meta = leaf if "kind" in leaf else client.classify(leaf, info)
        client.course_id = meta["course_id"] or client.course_id
        client.sku_id = meta["sku_id"] or client.sku_id
        LOG.info("---- [%d/%d] %s ----", idx, len(todo),
                 f"{leaf['chapter']} / {leaf['section']}" if leaf["chapter"] else leaf["name"])
        try:
            if meta["kind"] == "video":
                # 时长解析统一交给 watch_video（leaf_info → 进度 → mp4 → 同章估算）
                def _beat(pos: int, total: int, _idx=idx, _n=len(todo), _leaf=leaf) -> None:
                    if on_beat is not None:
                        try:
                            on_beat(_idx, _n, _leaf.get("name") or _leaf["leaf_id"], pos, total)
                        except Exception:  # noqa: BLE001
                            pass

                result = watch_video(client, leaf, duration=float(meta["duration"]), step=step,
                                     batch_size=batch_size, realtime=realtime,
                                     stop_event=stop_event, siblings=siblings,
                                     on_beat=_beat)
            elif meta["kind"] == "slide" and with_slide:
                result = watch_slide(client, leaf, slides=int(meta["slides"]))
            else:
                LOG.info("类型 %s 跳过（需要 --with-slide 或暂不支持）", meta["kind"])
                continue
        except AuthError:
            raise
        except TransientError as e:
            LOG.warning("网络/服务端异常：%s", e)
            result = "failed"
        stats[result] = stats.get(result, 0) + 1
        if on_leaf is not None:
            try:
                on_leaf(idx, len(todo), leaf.get("name") or leaf.get("leaf_id"), result)
            except Exception:  # noqa: BLE001
                LOG.debug("on_leaf 回调异常", exc_info=True)
        if result == "stopped":
            break
        time.sleep(random.uniform(*gap))
    return stats


def scan_course(
    client: Yuketang,
    course: Dict[str, Any],
    *,
    deep: bool = False,
    max_probe: int = 30,
    stop_event=None,
) -> Dict[str, Any]:
    """读一门课的概况，给 GUI 列表用。

    deep=False：只拉章节 + 一次整课进度（watched_record），不逐个 leaf_info，快；
    deep=True ：逐个 leaf_info，拿到精确类型/时长（52 个知识点大约 1~2 分钟）。
    """
    out: Dict[str, Any] = {
        "classroom_id": str(course["classroom_id"]),
        "name": course.get("name") or "",
        "chapters": 0, "leaves": 0, "videos": 0, "slides": 0, "others": 0,
        "done": 0, "duration": 0.0, "kind": "unknown", "error": "",
    }
    try:
        client.set_course(course["classroom_id"])
        try:
            chapters = client.chapters()
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            LOG.debug("章节接口报错：%s", e)
            chapters = []
        leaves = client.iter_leaves(chapters)
        out["chapters"] = len(chapters)
        out["leaves"] = len(leaves)
        if not leaves:
            alt = client.activity_leaves()
            if alt:
                leaves = alt
                out["kind"] = "activity"
        if not leaves:
            # 学习空间课（ai-workspace/lms-graph）
            try:
                from graphspace import GraphSpace
                gs = GraphSpace(client, course["classroom_id"])
                if gs.resolve():
                    nodes = gs.nodes()
                    vids = [n for n in nodes if n.get("video_id")] or gs.videos()
                    if vids:
                        out["kind"] = "graphspace"
                        out["leaves"] = len(vids)
                        out["videos"] = len(vids)
                        sit = {}
                        try:
                            sit = gs.study_situation()
                        except Exception:  # noqa: BLE001
                            sit = {}
                        try:
                            out["done"] = int(
                                sit.get("finished_count") or sit.get("finished") or
                                sit.get("study_count") or 0)
                        except (TypeError, ValueError):
                            out["done"] = 0
                        if not out["done"]:
                            out["done"] = sum(
                                1 for v in vids
                                if str((client.progress(v["video_id"]) or {}).get("completed")) == "1")
                        for v in vids:
                            d = v.get("duration")
                            try:
                                out["duration"] += float(d or 0)
                            except (TypeError, ValueError):
                                pass
                        out["graph_kg"] = gs.kg
                        out["nodes"] = len(nodes)
                        return out
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                LOG.debug("学习空间扫描失败：%s", e)
        if not leaves:
            out["kind"] = "classroom"
            return out

        rec = {}
        try:
            rec = client.watched_record()
        except Exception as e:  # noqa: BLE001
            LOG.debug("watched_record 失败：%s", e)
        out["done"] = sum(1 for l in leaves if str(rec.get(l["leaf_id"], {}).get("completed")) == "1")

        known = dict(client.profile.get("leaf_kinds") or {})
        # 非深度模式：每种 leaf_type 只抽一个样本问一次 leaf_info，把类型摸出来
        if not deep:
            for leaf in leaves:
                lt = str(leaf.get("leaf_type"))
                if lt in known:
                    continue
                try:
                    info = client.leaf_info(leaf["leaf_id"])
                    meta = client.classify(leaf, info)
                    known[lt] = meta["kind"]
                    if meta["duration"]:
                        out.setdefault("_dur_samples", []).append(float(meta["duration"]))
                except Exception:  # noqa: BLE001
                    known[lt] = "unknown"

        for i, leaf in enumerate(leaves):
            if stop_event is not None and stop_event.is_set():
                break
            if deep and i < max_probe:
                try:
                    info = client.leaf_info(leaf["leaf_id"])
                    meta = client.classify(leaf, info)
                except Exception:  # noqa: BLE001
                    meta = {"kind": "other", "duration": 0.0, "slides": 0,
                            "course_id": "", "sku_id": ""}
            else:
                kind = known.get(str(leaf.get("leaf_type")), "unknown")
                meta = {"kind": kind, "duration": 0.0, "slides": 0,
                        "course_id": "", "sku_id": ""}
            leaf.update(meta)
            kind = meta["kind"]
            if kind == "video":
                out["videos"] += 1
                out["duration"] += float(meta.get("duration") or 0)
            elif kind == "slide":
                out["slides"] += 1
            else:
                out["others"] += 1
        # 非深度模式下没逐个问时长：用样本均值把总时长补个估算值
        samples = out.pop("_dur_samples", [])
        if not deep and samples and out["videos"] > 0:
            avg = sum(samples) / len(samples)
            if out["duration"] < avg * out["videos"] * 0.5:
                out["duration"] = avg * out["videos"]
        # leaf_info 里 media.duration 常是 0（实测 2026-11），真时长在进度接口的 video_length
        if out["duration"] <= 0 and rec:
            total = 0.0
            for leaf in leaves:
                if leaf.get("kind") != "video":
                    continue
                try:
                    total += float((rec.get(leaf["leaf_id"]) or {}).get("video_length") or 0)
                except (TypeError, ValueError):
                    continue
            if total > 0:
                out["duration"] = total
            else:
                # 全都没看过：用播放地址的 mp4 头拿真实时长（最多探 8 个，避免太慢）
                out["duration"] = _probe_durations(client, leaves, cap=8, stop_event=stop_event)
        if not out["kind"] or out["kind"] == "unknown":
            out["kind"] = "mooc"
    except AuthError:
        raise
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:160]
    return out


def _probe_durations(client: Yuketang, leaves: List[Dict[str, Any]], cap: int = 8,
                     stop_event=None) -> float:
    """用播放地址的 mp4 头补时长：先探 cap 个样本，再按均值估算全课。"""
    try:
        from videometa import duration_from_playurl
    except Exception:  # noqa: BLE001
        return 0.0
    vids = [l for l in leaves if l.get("kind") == "video"]
    if not vids:
        return 0.0
    samples: List[float] = []
    for leaf in vids[:cap]:
        if stop_event is not None and stop_event.is_set():
            break
        cc = leaf.get("courseware_id")
        if not cc:
            continue
        try:
            dur = duration_from_playurl(client, str(cc))
        except Exception:  # noqa: BLE001
            dur = None
        if dur and dur > 0:
            samples.append(float(dur))
    if not samples:
        return 0.0
    avg = sum(samples) / len(samples)
    LOG.info("用 mp4 头探了 %d 个视频，均值 %.0fs，全课估算 %.0fs",
             len(samples), avg, avg * len(vids))
    return avg * len(vids)

def _kind_stats(leaves: List[Dict[str, Any]]) -> str:
    counter: Dict[str, int] = {}
    for l in leaves:
        counter[l.get("kind", "?")] = counter.get(l.get("kind", "?"), 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counter.items()))
