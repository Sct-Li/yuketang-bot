# -*- coding: utf-8 -*-
"""本地 mock v2：按真实雨课堂接口形状模拟，用来验证 自动识别 + 心跳补课 全链路。

    python tests/mock_server.py 8901
    python main.py --host http://127.0.0.1:8901 --cookies "csrftoken=x; sessionid=y; university_id=2803" --list
    python main.py --host http://127.0.0.1:8901 --cookies "..." --plan
    python main.py --host http://127.0.0.1:8901 --cookies "..." --auto --step 20 --batch 50
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CID = "7001"
COURSE_ID, SKU_ID, USER_ID = "9001", "3001", 12345
VIDEO_DURATION = 120.0

# 学习空间课（ai-workspace/lms-graph）用另一门课模拟
GRAPH_CID = "7002"
GRAPH_KG = "7777"
GRAPH_NODES = [
    {"id": 8101, "name": "绪论：为什么需要学习空间", "node_type": 1,
     "video_id": 9201, "leaf_id": 91001, "duration": 90},
    {"id": 8102, "name": "第二节 知识图谱", "node_type": 1,
     "video_id": 9202, "leaf_id": 91002, "duration": 60},
]

CHAPTERS = {
    "data": {"course_chapter": [
        {"name": "第一章 导论", "section_leaf_list": [
            {"name": "1.1 概念", "leaf_list": [
                {"id": 5001, "name": "1.1.1 视频：什么是全球化", "leaf_type": 0},
                {"id": 5002, "name": "1.1.2 课件：全球化简史", "leaf_type": 1},
            ]},
        ]},
        {"name": "第二章", "section_leaf_list": [
            {"name": "2.1 演进", "leaf_list": [
                {"id": 5003, "name": "2.1.1 视频：三次浪潮", "leaf_type": 0},
            ]},
        ]},
    ]}
}

PROGRESS: dict = {}   # video_id -> {"cp": float, "rate": float, "completed": int}
HEARTBEATS: list = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _q(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def do_GET(self):
        p = urllib.parse.urlsplit(self.path).path
        q = self._q()
        if p == "/v2/api/web/userinfo":
            return self._json({"errmsg": "Success", "data": [
                {"user_id": USER_ID, "name": "测试同学", "university_id": 2803}]})
        if p == "/v2/api/web/courses/list":
            return self._json({"errmsg": "Success", "data": {"list": [
                {"classroom_id": CID, "name": "全球化与世界空间-2025秋",
                 "course": {"name": "全球化与世界空间"}, "term": "2025秋", "role": "学生"},
                {"classroom_id": GRAPH_CID, "name": "大学生应急救援-学习空间",
                 "course": {"name": "大学生应急救援"}, "term": "2025秋", "role": "学生"},
            ]}})
        if p == "/mooc-api/v1/lms/learn/course/chapter":
            if q.get("cid", [""])[0] != CID:
                return self._json({"error_code": 80019, "msg": "not purchased"})
            return self._json(CHAPTERS)
        if p.startswith("/mooc-api/v1/lms/learn/leaf_info/"):
            leaf_id = p.rstrip("/").split("/")[-1]
            if leaf_id == "5002":
                return self._json({"success": True, "data": {
                    "user_id": USER_ID, "course_id": COURSE_ID, "sku_id": SKU_ID,
                    "content_info": {"cards": [{"id": i} for i in range(6)]}}})
            return self._json({"success": True, "data": {
                "user_id": USER_ID, "course_id": COURSE_ID, "sku_id": SKU_ID,
                "content_info": {"media": {"duration": VIDEO_DURATION, "name": "video"}}}})
        if p == "/video-log/get_video_watch_progress/":
            vid = (q.get("video_id") or [""])[0]
            st = PROGRESS.get(vid, {"cp": 0.0, "rate": 0.0, "completed": 0})
            return self._json({"data": {vid: {
                "rate": st["rate"], "completed": st["completed"],
                "watch_length": st["cp"], "video_length": VIDEO_DURATION,
                "last_point": st["cp"]}}})
        if p == "/video-log/get_video_watched_record/":
            return self._json({k: {"rate": v["rate"], "completed": v["completed"]}
                               for k, v in PROGRESS.items()})
        if p == "/v2/api/web/logs/learn/" + CID:
            return self._json({"errmsg": "Success", "data": {"activities": [
                {"type": 17, "content": {"leaf_id": 5001}, "courseware_id": 88001},
                {"type": 17, "content": {"leaf_id": 5003}, "courseware_id": 88003},
            ]}})
        # ---- 学习空间（ai-workspace / lms-graph） ----
        if p == "/train_platform/v1/knowledge_graph/classroom_relate_kg_info/":
            if (q.get("classroom_id") or [""])[0] == GRAPH_CID:
                return self._json({"success": True, "data": {"graph_id": GRAPH_KG, "name": "图谱课"}})
            return self._json({"success": False, "data": None})
        if p == f"/train_platform/v1/knowledge_graph/{GRAPH_KG}/nodes/":
            return self._json({"success": True, "data": {"nodes": GRAPH_NODES}})
        if p == f"/train_platform/v1/knowledge_graph/{GRAPH_KG}/video_info/":
            vid = (q.get("video_id") or [""])[0]
            for n in GRAPH_NODES:
                if str(n["video_id"]) == vid:
                    return self._json({"success": True, "data": {
                        "video_id": n["video_id"], "duration": n["duration"], "name": n["name"]}})
            return self._json({"success": True, "data": {}})
        if p == f"/train_platform/v1/knowledge_graph/{GRAPH_KG}/student/study_situation/":
            return self._json({"success": True, "data": {
                "finished_count": sum(1 for n in GRAPH_NODES if n.get("_done"))}})
        if p.startswith("/train_platform/v1/knowledge_graph/") and "add_study_node_and_leaf_h5" in p:
            return self._json({"success": True, "data": {"ok": 1}})
        return self._json({"error_code": 404, "path": p}, 404)

    def do_POST(self):
        p = urllib.parse.urlsplit(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            body = json.loads(raw or "{}")
        except ValueError:
            body = {}
        # 学习空间：标记节点已学
        if p.startswith("/train_platform/v1/knowledge_graph/") and "add_study_node_and_leaf_h5" in p:
            node = p.rstrip("/").split("/")[-1]
            for nd in GRAPH_NODES:
                if str(nd["id"]) == node:
                    nd["_done"] = True
            print(f"graph mark node {node} body={json.dumps(body, ensure_ascii=False)}", flush=True)
            return self._json({"success": True, "data": {"ok": 1}})
        if p == "/c27/online_courseware/xty/instance/add_study_leaf/":
            return self._json({"success": True, "data": {}})
        if p == "/video-log/heartbeat/":
            entries = body.get("heart_data") or []
            HEARTBEATS.extend(entries)
            for e in entries:
                vid = str(e.get("v"))
                st = PROGRESS.setdefault(vid, {"cp": 0.0, "rate": 0.0, "completed": 0})
                st["cp"] = max(st["cp"], float(e.get("cp") or 0))
                st["rate"] = min(1.0, st["cp"] / VIDEO_DURATION)
                if st["rate"] >= 0.98:
                    st["completed"] = 1
            print(f"heartbeat += {len(entries)}  progress={json.dumps(PROGRESS, ensure_ascii=False)}", flush=True)
            if HEARTBEATS:
                print("  sample:", json.dumps(HEARTBEATS[-1], ensure_ascii=False), flush=True)
            return self._json({"errmsg": "Success", "data": {}})
        return self._json({"error_code": 404}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    print(f"mock v2 on http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
