# -*- coding: utf-8 -*-
"""学习空间（ai-workspace / lms-graph）支持。

雨课堂升级后，一部分课程点进去会跳到：
    /ai-workspace/lms-graph/{classroom_id}/video/{video_id}?is_chapter=1&node_id={node_id}
这类课程在老的 leaf 章节接口里是空的，内容在「知识图谱」里：
    课程(classroom) → 图谱实例(graph_id) → 节点(node) → 视频(video)/章节叶子(leaf)

接口全部来自官方 aiworkspace 包体（dump/aiws/api_map.json）：

    classroom_relate_kg_info   /train_platform/v1/knowledge_graph/classroom_relate_kg_info/
    instance_list              /train_platform/v1/knowledge_graph/instance_list/
    student_switch             /train_platform/v1/knowledge_graph/{pk}/student_switch/{classroom_id}/
    nodes                      /train_platform/v1/knowledge_graph/{pk}/nodes/
    simple_nodes               /train_platform/v1/knowledge_graph/{pk}/simple_nodes/
    node_detail_stu            /train_platform/v1/knowledge_graph/{kg}/node_detail_stu/{node}/{cid}/
    relate_classroom_leaf      /train_platform/v1/knowledge_graph/{kg}/student/relate_classroom_leaf/{node}/
    video_info                 /train_platform/v1/knowledge_graph/{kg}/video_info/
    study_situation            /train_platform/v1/knowledge_graph/{kg}/student/study_situation/
    add_study_node_and_leaf_h5 /train_platform/v1/knowledge_graph/{kg}/add_study_node_and_leaf_h5/{node}/
    add_study_leaf(xty)        /c27/online_courseware/xty/instance/add_study_leaf/
    latest_study_node          /train_platform/v1/knowledge_graph/{kg}/latest_study_node/{cid}/

视频播放进度仍然是老的 /video-log/heartbeat/（aiworkspace 包体里出现两处），
所以图谱视频的心跳沿用 ykt.heartbeat_entry，只把 leaf_id 换成 video_id。

注意：图谱接口的 query/body 字段没有公开文档，本模块用「多候选 + 容错解析」的方式探测，
真实字段以 probe.py --graph 的 dump 为准；明确拿不到时返回空而不是瞎猜。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from ykt import AuthError, Yuketang, deep_first, iter_dicts

LOG = logging.getLogger("graph")

API = {
    "relate_kg": "/train_platform/v1/knowledge_graph/classroom_relate_kg_info/",
    "instance_list": "/train_platform/v1/knowledge_graph/instance_list/",
    "student_switch": "/train_platform/v1/knowledge_graph/{kg}/student_switch/{cid}/",
    "nodes": "/train_platform/v1/knowledge_graph/{kg}/nodes/",
    "simple_nodes": "/train_platform/v1/knowledge_graph/{kg}/simple_nodes/",
    "node_detail_stu": "/train_platform/v1/knowledge_graph/{kg}/node_detail_stu/{node}/{cid}/",
    "relate_classroom_leaf": "/train_platform/v1/knowledge_graph/{kg}/student/relate_classroom_leaf/{node}/",
    "video_info": "/train_platform/v1/knowledge_graph/{kg}/video_info/",
    "study_situation": "/train_platform/v1/knowledge_graph/{kg}/student/study_situation/",
    "add_study_node": "/train_platform/v1/knowledge_graph/{kg}/add_study_node_and_leaf_h5/{node}/",
    "add_study_leaf_xty": "/c27/online_courseware/xty/instance/add_study_leaf/",
    "latest_study_node": "/train_platform/v1/knowledge_graph/{kg}/latest_study_node/{cid}/",
}


def _names(d: Dict[str, Any]) -> str:
    return str(d.get("name") or d.get("title") or d.get("node_name") or "")


class GraphSpace:
    """一门课程的「学习空间」视图。"""

    def __init__(self, client: Yuketang, classroom_id: Any) -> None:
        self.client = client
        self.cid = str(classroom_id)
        self.kg: Optional[str] = None
        self._nodes: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------ 基础请求
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return self.client.get_json(path, params=params)
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            LOG.debug("graph GET %s -> %s", path, e)
            return None

    def _post(self, path: str, body: Any) -> Any:
        try:
            return self.client.post_json(path, body)
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            LOG.debug("graph POST %s -> %s", path, e)
            return None

    # ------------------------------------------------------------ 图谱 id
    def resolve(self, force: bool = False) -> Optional[str]:
        """classroom_id -> graph_id（kg id）。"""
        if self.kg and not force:
            return self.kg
        # 1) 直接问 classroom 关联
        for params in (
            {"classroom_id": self.cid},
            {"classroom_id": self.cid, "term": "latest"},
        ):
            data = self._get(API["relate_kg"], params)
            kg = self._pick_kg(data)
            if kg:
                self.kg = kg
                LOG.info("图谱：classroom %s -> kg %s", self.cid, kg)
                return kg
        # 2) 实例列表里筛 classroom
        data = self._get(API["instance_list"], {"classroom_id": self.cid})
        kg = self._pick_kg(data)
        if kg:
            self.kg = kg
            LOG.info("图谱：classroom %s -> kg %s（instance_list）", self.cid, kg)
            return kg
        # 3) student_switch 显式切一下再问
        for method in ("POST", "GET"):
            try:
                path = API["student_switch"].format(kg=0, cid=self.cid)
                data = (self.client.post_json(path, {"classroom_id": self.cid}) if method == "POST"
                        else self._get(path))
            except AuthError:
                raise
            except Exception:  # noqa: BLE001
                data = None
            kg = self._pick_kg(data)
            if kg:
                self.kg = kg
                LOG.info("图谱：classroom %s -> kg %s（student_switch）", self.cid, kg)
                return kg
        LOG.warning("识别不到该课程的图谱 id（可能这门课不是学习空间课）")
        return None

    @staticmethod
    def _pick_kg(data: Any) -> Optional[str]:
        if not isinstance(data, (dict, list)):
            return None
        for key in ("graph_id", "kg_id", "kgid", "id", "pk"):
            val = deep_first(data, (key,))
            if isinstance(val, (int, str)) and str(val).isdigit():
                return str(val)
        for d in iter_dicts(data):
            for k in ("graph_id", "kg_id", "kgid"):
                if str(d.get(k, "")).isdigit():
                    return str(d[k])
        return None

    # ------------------------------------------------------------ 节点 / 视频
    def nodes(self, force: bool = False) -> List[Dict[str, Any]]:
        if self._nodes is not None and not force:
            return self._nodes
        kg = self.resolve()
        if not kg:
            self._nodes = []
            return []
        out: List[Dict[str, Any]] = []
        for key in ("nodes", "simple_nodes"):
            for params in (
                {"classroom_id": self.cid},
                {"classroom_id": self.cid, "with_leaf": 1},
                {"classroom_id": self.cid, "need_leaf": "true"},
            ):
                data = self._get(API[key].format(kg=kg), params)
                got = self._extract_nodes(data)
                if got:
                    out = got
                    LOG.info("图谱 %s：%s 拿到 %d 个节点", kg, key, len(got))
                    break
            if out:
                break
        self._nodes = out
        return out

    @staticmethod
    def _extract_nodes(data: Any) -> List[Dict[str, Any]]:
        if not isinstance(data, (dict, list)):
            return []
        out: List[Dict[str, Any]] = []
        seen = set()

        def walk(item: Any) -> None:
            if isinstance(item, list):
                for x in item:
                    walk(x)
                return
            if not isinstance(item, dict):
                return
            nid = item.get("node_id") or item.get("id") or item.get("node_pk")
            name = _names(item)
            if nid is not None and (name or item.get("video_id") or item.get("leaf_id")):
                key = str(nid)
                if key not in seen:
                    seen.add(key)
                    out.append({
                        "node_id": key,
                        "name": name or f"node-{key}",
                        "type": item.get("node_type") or item.get("type") or item.get("kind"),
                        "video_id": item.get("video_id") or item.get("videoId"),
                        "leaf_id": item.get("leaf_id") or item.get("leafId"),
                        "raw": item,
                    })
            for v in item.values():
                if isinstance(v, (list, dict)):
                    walk(v)

        walk(data)
        return out

    def videos(self, force: bool = False) -> List[Dict[str, Any]]:
        """节点里带 video_id 的挑出来；没有就直接问 video_info。"""
        nodes = self.nodes(force=force)
        vids = [n for n in nodes if n.get("video_id")]
        if vids:
            return vids
        kg = self.kg
        if not kg:
            return []
        for params in (
            {"classroom_id": self.cid},
            {"classroom_id": self.cid, "is_student": 1},
            {"classroom_id": self.cid, "node_id": None},
        ):
            data = self._get(API["video_info"].format(kg=kg), params)
            if isinstance(data, (dict, list)):
                found: List[Dict[str, Any]] = []
                for d in iter_dicts(data):
                    vid = d.get("video_id") or d.get("videoId") or d.get("id")
                    if vid is None:
                        continue
                    if not (d.get("duration") or d.get("name") or d.get("title") or d.get("url")):
                        continue
                    found.append({
                        "node_id": str(d.get("node_id") or ""),
                        "name": _names(d) or f"video-{vid}",
                        "video_id": vid,
                        "leaf_id": d.get("leaf_id"),
                        "duration": d.get("duration"),
                        "raw": d,
                    })
                if found:
                    seen, uniq = set(), []
                    for x in found:
                        k = str(x["video_id"])
                        if k in seen:
                            continue
                        seen.add(k)
                        uniq.append(x)
                    return uniq
        return []

    def video_detail(self, video_id: Any, node_id: Any = None) -> Dict[str, Any]:
        kg = self.resolve()
        if not kg:
            return {}
        base = {"video_id": video_id, "classroom_id": self.cid}
        variants = [dict(base), dict(base, node_id=node_id), dict(base, node_id=node_id, with_progress=1)]
        for params in variants:
            data = self._get(API["video_info"].format(kg=kg), {k: v for k, v in params.items() if v is not None})
            if isinstance(data, dict):
                d = data.get("data") if isinstance(data.get("data"), dict) else data
                if d:
                    return d
        return {}

    # ------------------------------------------------------------ 学习状态 / 上报
    def node_detail(self, node_id: Any) -> Dict[str, Any]:
        kg = self.resolve()
        if not kg:
            return {}
        data = self._get(API["node_detail_stu"].format(kg=kg, node=node_id, cid=self.cid))
        if isinstance(data, dict):
            d = data.get("data")
            return d if isinstance(d, dict) else data
        return {}

    def study_situation(self) -> Dict[str, Any]:
        kg = self.resolve()
        if not kg:
            return {}
        data = self._get(API["study_situation"].format(kg=kg), {"classroom_id": self.cid})
        if isinstance(data, dict):
            d = data.get("data")
            return d if isinstance(d, dict) else data
        return {}

    def mark_studied(self, node_id: Any, leaf_id: Any = None) -> Dict[str, Any]:
        """把某个节点/叶子标记为已学习（官方 H5 就是这么做的）。

        返回 {"ok": bool, "how": str, "resp": ...}；两个候选接口都打一遍，
        谁先返回明确成功就用谁，都失败则把最后一次响应带回去供排查。
        """
        kg = self.resolve()
        if not kg:
            return {"ok": False, "how": "no-graph", "resp": None}
        body_variants = [
            {"classroom_id": self.cid, "node_id": node_id, "leaf_id": leaf_id},
            {"classroom_id": self.cid, "node_id": node_id},
            {"node_id": node_id, "leaf_id": leaf_id, "classroom_id": self.cid, "study_time": 1},
        ]
        last = None
        for body in body_variants:
            body = {k: v for k, v in body.items() if v is not None}
            last = self._post(API["add_study_node"].format(kg=kg, node=node_id), body)
            if _is_ok(last):
                return {"ok": True, "how": "add_study_node_and_leaf_h5", "resp": last}
        if leaf_id is not None:
            last = self._post(API["add_study_leaf_xty"], {
                "leaf_id": leaf_id, "classroom_id": self.cid, "study_time": 1,
            })
            if _is_ok(last):
                return {"ok": True, "how": "xty/add_study_leaf", "resp": last}
        return {"ok": False, "how": "unknown", "resp": last}

    def latest_node(self) -> Dict[str, Any]:
        kg = self.resolve()
        if not kg:
            return {}
        data = self._get(API["latest_study_node"].format(kg=kg, cid=self.cid))
        return data if isinstance(data, dict) else {}


def _is_ok(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    if resp.get("success") is True or resp.get("ok") is True:
        return True
    for key in ("errcode", "err_no", "code", "status", "status_code"):
        if key in resp:
            try:
                return int(resp[key]) in (0, 200)
            except (TypeError, ValueError):
                return False
    if resp.get("errmsg") == "Success":
        return True
    return False


def summarize(client: Yuketang, classroom_id: Any) -> Dict[str, Any]:
    """给扫描/GUI 用的一句话概况。"""
    gs = GraphSpace(client, classroom_id)
    kg = gs.resolve()
    nodes = gs.nodes() if kg else []
    vids = [n for n in nodes if n.get("video_id")] if nodes else gs.videos()
    return {
        "kg": kg,
        "nodes": len(nodes),
        "videos": len(vids),
        "names": [n.get("name") for n in vids[:3]],
    }


def watch_graph_videos(
    client: Yuketang,
    gs: GraphSpace,
    *,
    step: int = 20,
    batch_size: int = 50,
    realtime: bool = False,
    stop_event=None,
    on_leaf=None,
) -> Dict[str, int]:
    """把学习空间里的视频按心跳刷完，再用图谱接口标记节点已学。

    兜底顺序：心跳（/video-log/heartbeat/，v=video_id）→ 校验进度 → 图谱
    add_study_node_and_leaf_h5 显式标记。任何一步拿不到明确信号都如实记 failed。
    """
    from ykt import TransientError  # 局部导入避免循环

    vids = gs.videos()
    stats = {"done": 0, "skipped": 0, "failed": 0, "stopped": 0, "marked": 0}
    total = len(vids)
    LOG.info("学习空间：%d 个视频（kg=%s）", total, gs.kg)
    for idx, item in enumerate(vids, 1):
        if stop_event is not None and stop_event.is_set():
            stats["stopped"] = total - idx + 1
            break
        vid = item.get("video_id")
        node = item.get("node_id")
        name = item.get("name") or f"video-{vid}"
        try:
            detail = gs.video_detail(vid, node)
            # 图谱视频的心跳 c 字段要课程 id；video_info 里有就用，没有退回 classroom_id
            if not client.course_id:
                client.course_id = str(detail.get("course_id") or detail.get("cid") or gs.cid)
            duration = 0.0
            for key in ("duration", "video_length", "length"):
                try:
                    duration = float(detail.get(key) or item.get("duration") or 0)
                    break
                except (TypeError, ValueError):
                    continue
            if duration > 10000:
                duration /= 1000.0
            if duration <= 0:
                LOG.warning("《%s》拿不到时长，跳过心跳直接标记", name)
            else:
                prog = client.progress(vid) if client.course_id else {}
                if client.is_completed(prog):
                    stats["skipped"] += 1
                    LOG.info("《%s》已完成 %.0f%%，跳过", name, client.rate(prog) * 100)
                    if on_leaf:
                        on_leaf(idx, total, name, "skipped")
                    continue
                pos, sq, study = 0.0, 1, 0.0
                import random
                import time as _t
                pg = f"g{vid}_{random.randint(10000, 99999)}"
                ts_base = int(_t.time() * 1000)
                wall = _t.time()
                while pos < duration:
                    if stop_event is not None and stop_event.is_set():
                        break
                    entries = []
                    while pos < duration and len(entries) < batch_size:
                        pos = min(duration, pos + step)
                        entries.append(client.heartbeat_entry(
                            et="videoend" if pos >= duration else "loadeddata",
                            cp=pos, sq=sq, ts_ms=ts_base + sq * 300, pg=pg,
                            leaf_id=vid, courseware_id=item.get("leaf_id") or vid,
                            api_user_id=client.user_id, duration=duration,
                        ))
                        sq += 1
                    if realtime:
                        drift = wall + pos - _t.time()
                        if drift > 0:
                            _t.sleep(min(drift, 120))
                    if entries and not client.send_heartbeat(entries):
                        _t.sleep(1.5)
                        client.send_heartbeat(entries)
                    study += step * len(entries)
                    _t.sleep(0.4)
            marked = gs.mark_studied(node, item.get("leaf_id"))
            if marked.get("ok"):
                stats["marked"] += 1
                stats["done"] += 1
                LOG.info("《%s》已标记学习（%s）", name, marked.get("how"))
            else:
                prog = client.progress(vid) if client.course_id else {}
                if client.is_completed(prog):
                    stats["done"] += 1
                else:
                    stats["failed"] += 1
                    LOG.warning("《%s》没能确认学习状态：heartbeat=%s mark=%s",
                                name, client.rate(prog), marked.get("how"))
            if on_leaf:
                on_leaf(idx, total, name, "done" if stats["done"] else "failed")
        except TransientError as e:
            stats["failed"] += 1
            LOG.warning("《%s》网络异常：%s", name, e)
        except AuthError:
            raise
    return stats
