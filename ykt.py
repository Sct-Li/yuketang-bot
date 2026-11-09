# -*- coding: utf-8 -*-
"""长江雨课堂 协议层 v2 —— 真实接口 + 自动识别。

接口来源：官方前端 player.newHeartlog() 逆向 + fe-static-yuketang 1.2.346 包体扫描，
参考实现在 dump/ref/main.py（字段与请求头可对照）。

自动识别（--auto / 首次运行自动触发）会做这几件事，然后把结果写进 profile.json：
  1. 认证探针：/v2/api/web/userinfo → user_id、name、uv_id（university_id）
  2. 课程列表探针：多个候选路径，取第一个返回非空 list 的，记录 shape
  3. 课程树探针：/mooc-api/v1/lms/learn/course/chapter → leaf 结构，记录 leaf_type 分布
  4. 知识点类型判定：不写死 leaf_type，用 leaf_info 的 content_info 实际内容判定
     video(duration>0) / slide(有 cards|slides|pages) / other(考试、讨论、直播)
  5. 进度接口探针：/video-log/get_video_watch_progress 是否返回 rate/completed
  6. 心跳接口探针：POST /video-log/heartbeat/ 是否 200
识别结果缓存，后续运行直接复用；`--redetect` 强制重跑。

纯标准库。
"""
from __future__ import annotations

import gzip
import http.client
import io
import json
import logging
import os
import random
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOG = logging.getLogger("ykt")

HOST = "https://changjiang.yuketang.cn"
LEAF_VIDEO = 0

BASE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 候选接口表：自动识别时按顺序探测，第一个有效的写入 profile
CANDIDATES: Dict[str, List[str]] = {
    "userinfo": ["/v2/api/web/userinfo", "/api/v3/user/basic-info", "/v2/api/web/user/info"],
    "courses": [
        "/v2/api/web/courses/list?identity=2",
        "/v2/api/web/courses/list?identity_id={uv_id}&page=1&page_size=100",
    ],
    "classroom_meta": [
        "/v2/api/web/classrooms/{cid}?role=5&term=latest&uv_id={uv_id}&classroom_id={cid}",
        "/v2/api/web/classrooms/{cid}?role=5",
    ],
    "chapters": [
        "/mooc-api/v1/lms/learn/course/chapter?cid={cid}&sign={sign}&classroom_id={cid}&show_unpublished=0",
        "/mooc-api/v1/lms/learn/course/chapter?cid={cid}&sign={sign}&term=latest&uv_id={uv_id}&classroom_id={cid}",
        "/mooc-api/v1/lms/learn/course/chapter?cid={cid}",
        "/api/v3/course/{cid}/chapter/list?classroom_id={cid}",
        "/v2/api/web/courses/{cid}/chapters",
    ],
    "leaf_info": [
        "/mooc-api/v1/lms/learn/leaf_info/{cid}/{leaf_id}/?classroom_id={cid}",
        "/mooc-api/v1/lms/learn/leaf_info/{cid}/{leaf_id}/",
        "/api/v3/lesson/lesson-info?lesson_id={leaf_id}&classroom_id={cid}",
    ],
    "leaf_info": [
        "/mooc-api/v1/lms/learn/leaf_info/{cid}/{leaf_id}/",
        "/api/v3/lesson/lesson-info?lesson_id={leaf_id}&classroom_id={cid}",
    ],
    "progress": ["/video-log/get_video_watch_progress/"],
    "watched": ["/video-log/get_video_watched_record/"],
    "heartbeat": ["/video-log/heartbeat/"],
    "activities": ["/v2/api/web/logs/learn/{cid}?actype=-1&page=0&offset=100&sort=-1"],
}


class AuthError(RuntimeError):
    """cookie 失效 / 未登录。"""


class TransientError(RuntimeError):
    """5xx / 429 / 网络抖动，可重试。"""


# ---------------------------------------------------------------------------
# 容错工具
# ---------------------------------------------------------------------------
def iter_dicts(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_dicts(v)


def deep_first(obj: Any, keys: Iterable[str], default: Any = None) -> Any:
    keys = set(keys)
    for d in iter_dicts(obj):
        for k in keys:
            if k in d and d[k] not in (None, "", [], {}):
                return d[k]
    return default


def count_slide_like(info: Any) -> int:
    """在 leaf_info 里数幻灯片/卡片页数，兼容 cards / slides / pages / ppt。"""
    best = 0
    for d in iter_dicts(info):
        for k, v in d.items():
            if k.lower() in ("cards", "slides", "pages", "ppt_slides", "page_list") and isinstance(v, list):
                best = max(best, len(v))
    return best


def rand_token(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


# ---------------------------------------------------------------------------
class Yuketang:
    def __init__(
        self,
        csrftoken: str,
        sessionid: str,
        uv_id: str = "",
        host: str = HOST,
        timeout: float = 25.0,
        jitter: Tuple[float, float] = (0.0, 0.06),
        profile_path: Optional[Path] = None,
        pace: Optional[float] = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.csrftoken = csrftoken.strip()
        self.sessionid = sessionid.strip()
        self.uv_id = str(uv_id or "").strip()
        self.timeout = timeout
        self.jitter = jitter          # 每次请求前的小抖动（默认 0~60ms，别再加了）
        self.pace = pace              # POST/心跳的额外间隔（None=按 method 自适应）
        self.profile_path = profile_path
        self._pool: Dict[Tuple[str, Optional[str], Optional[int]], Any] = {}
        self.profile: Dict[str, Any] = self._load_profile()
        self.endpoints: Dict[str, str] = dict(self.profile.get("endpoints") or {})
        self.user_id: Optional[Any] = None
        self.login_name: Optional[str] = None
        self.cid: Optional[str] = None           # classroom_id
        self.course_id: Optional[str] = None     # MOOC course_id（心跳 c 字段）
        self.sku_id: Optional[str] = None
        self._courseware: Dict[str, str] = {}
        self._durations: Dict[str, float] = {}

    # ---------------------------------------------------------- profile
    def _load_profile(self) -> Dict[str, Any]:
        if self.profile_path and self.profile_path.exists():
            try:
                data = json.loads(self.profile_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    return data
            except ValueError:
                LOG.warning("profile.json 解析失败，忽略")
        return {}

    def save_profile(self) -> None:
        if not self.profile_path:
            return
        payload = {
            "host": self.host,
            "uv_id": self.uv_id,
            "user_id": self.user_id,
            "user_name": self.login_name,
            "endpoints": self.endpoints,
            "leaf_kinds": self.profile.get("leaf_kinds") or {},
            "detected_at": int(time.time()),
        }
        self.profile_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("识别结果已写入 %s", self.profile_path)

    # ---------------------------------------------------------- 请求层
    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        cookie = (
            f"csrftoken={self.csrftoken}; sessionid={self.sessionid}"
            + (f"; university_id={self.uv_id}" if self.uv_id else "")
            + "; platform_id=3"
        )
        h = {
            "User-Agent": BASE_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "referer": f"{self.host}/v2/web/index",
            "x-csrftoken": self.csrftoken,
            "xt-agent": "web",
            "xtbz": "ykt",          # 必须是 ykt，cloud 会被判未购买（80019/80005）
            "university-id": self.uv_id,
            "uv-id": self.uv_id,
        }
        if self.cid:
            h["classroom-id"] = str(self.cid)
        if extra:
            h.update(extra)
        return h

    def _conn(self, parts):
        key = (parts.scheme, parts.hostname, parts.port)
        conn = self._pool.get(key)
        if conn is None:
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = cls(parts.hostname, parts.port, timeout=self.timeout)
            self._pool[key] = conn
        return conn

    def _drop(self, parts) -> None:
        conn = self._pool.pop((parts.scheme, parts.hostname, parts.port), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        for conn in list(self._pool.values()):
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._pool.clear()

    def request(
        self,
        method: str,
        path: str,
        params=None,
        body=None,
        retries: int = 3,
        timeout=None,
    ):
        """带连接复用 + 轻量抖动的请求。

        性能要点（2026-11 优化）：
          · 复用 http.client 连接（keep-alive），省掉每次 TLS 握手的 150~250ms
          · 抖动默认只有 0~60ms，不再无脑 sleep 1~2 秒
          · POST/心跳额外加 ~120ms 间隔，避免瞬时并发被风控
        """
        url = path if path.startswith("http") else self.host + path
        if params:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True
            )
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None

        parts = urllib.parse.urlsplit(url)
        target = parts.path + (("?" + parts.query) if parts.query else "")
        headers = self._headers()

        last = None
        for attempt in range(1, retries + 1):
            try:
                if self.jitter and self.jitter[1] > 0:
                    time.sleep(random.uniform(*self.jitter))
                if method != "GET":
                    time.sleep(self.pace if self.pace is not None else 0.12)
                conn = self._conn(parts)
                conn.request(method, target, body=raw_body, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                enc = resp.getheader("Content-Encoding", "") or ""
                raw = resp.read()
                if enc.lower() == "gzip":
                    try:
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                    except OSError:
                        pass
                text = raw.decode("utf-8", "replace")
                if status in (401, 403):
                    raise AuthError(f"{status} {method} {url} -> {text[:120]}")
                if status >= 500 or status == 429:
                    self._drop(parts)
                    raise TransientError(f"{status}")
                try:
                    data = json.loads(text) if text else {}
                except ValueError:
                    data = {"_raw": text}
                return status, data
            except AuthError:
                raise
            except (http.client.HTTPException, urllib.error.URLError, TimeoutError,
                    TransientError, OSError) as e:
                last = e
                self._drop(parts)
                wait = 0.8 * attempt + random.uniform(0, 0.6)
                LOG.debug("retry %s %s (%.1fs): %s", method, url, wait, e)
                time.sleep(wait)
        raise TransientError(f"{method} {url} failed: {last}")

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        _, data = self.request("GET", path, params=params)
        return data

    def post_json(self, path: str, body: Any) -> Any:
        _, data = self.request("POST", path, body=body)
        return data

    # ---------------------------------------------------------- 自动识别
    def _probe(self, name: str, **fmt: Any) -> Optional[Tuple[str, Any]]:
        """按候选表探测，返回 (命中的模板, 响应)。"""
        fmt.setdefault("uv_id", self.uv_id or 0)
        fmt.setdefault("cid", 0)
        fmt.setdefault("leaf_id", 0)
        for tpl in CANDIDATES[name]:
            path = tpl.format(**fmt)
            try:
                status, data = self.request("GET", path, retries=1)
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                LOG.debug("probe %s -> %s: %s", name, path, e)
                continue
            if status < 400 and data not in (None, {}, []):
                return tpl, data
        return None

    def detect(self, redetect: bool = False) -> Dict[str, Any]:
        """自动识别账号 / 接口 / 课程结构。幂等，结果缓存到 profile.json。"""
        if self.endpoints and not redetect:
            self.user_id = self.profile.get("user_id")
            self.uv_id = self.uv_id or str(self.profile.get("uv_id") or "")
            self.login_name = self.profile.get("user_name")
            LOG.info("复用 profile：user=%s uv_id=%s", self.login_name, self.uv_id)
            return self.profile

        hit = self._probe("userinfo")
        if not hit:
            raise AuthError("认证探针全部失败：csrftoken / sessionid 无效或已过期")
        tpl, data = hit
        self.endpoints["userinfo"] = tpl
        info = {}
        if isinstance(data.get("data"), list) and data["data"]:
            info = data["data"][0] if isinstance(data["data"][0], dict) else {}
        elif isinstance(data.get("data"), dict):
            info = data["data"]
        self.user_id = info.get("user_id") or deep_first(data, ("user_id", "id"))
        self.login_name = info.get("name") or deep_first(data, ("name", "realname"))
        self.uv_id = self.uv_id or str(
            info.get("university_id") or deep_first(data, ("university_id", "school_id")) or ""
        )
        LOG.info("认证成功：%s（user_id=%s, uv_id=%s）", self.login_name, self.user_id, self.uv_id)

        for name in ("courses", "chapters", "leaf_info", "progress", "watched", "heartbeat", "activities"):
            if name == "chapters":
                continue  # 需要 cid，放到选课后探测
            hit = self._probe(name) if name != "heartbeat" else None
            if hit:
                self.endpoints[name] = hit[0]
                LOG.info("接口识别 %-10s -> %s", name, hit[0])

        if "courses" not in self.endpoints:
            hit = self._probe("courses")
            if hit:
                self.endpoints["courses"] = hit[0]
        if "courses" not in self.endpoints:
            raise RuntimeError("课程列表接口识别失败，用 --dump 看原始响应")

        self.profile = {
            "host": self.host,
            "uv_id": self.uv_id,
            "user_id": self.user_id,
            "user_name": self.login_name,
            "endpoints": self.endpoints,
            "leaf_kinds": {},
        }
        self.save_profile()
        return self.profile

    # ---------------------------------------------------------- 课程 / 结构
    def list_courses(self) -> List[Dict[str, Any]]:
        tpl = self.endpoints.get("courses") or CANDIDATES["courses"][0]
        data = self.get_json(tpl.format(uv_id=self.uv_id or 0))
        out: List[Dict[str, Any]] = []
        items: List[Any] = []
        d = data.get("data") if isinstance(data, dict) else None
        if isinstance(d, dict) and isinstance(d.get("list"), list):
            items = d["list"]
        elif isinstance(d, list):
            items = d
        if not items:
            items = [x for x in iter_dicts(data) if x.get("classroom_id") and (x.get("course") or x.get("name"))]
        for item in items:
            if not isinstance(item, dict):
                continue
            course = item.get("course") or {}
            cid = item.get("classroom_id") or item.get("classroomId") or deep_first(item, ("classroom_id",))
            if cid is None:
                continue
            out.append({
                "classroom_id": str(cid),
                "name": course.get("name") or item.get("name") or "",
                "full_name": item.get("name") or "",
                "term": item.get("term") or "",
                "role": item.get("role") or "",
            })
        return out

    def set_course(self, classroom_id: Any) -> None:
        self.cid = str(classroom_id)
        self.sign = ""
        self.endpoints.pop("classroom_meta_ok", None)
        LOG.debug("课程上下文 classroom_id=%s", self.cid)

    def classroom_meta(self) -> Dict[str, Any]:
        """课程元信息：course_sign / course_id / sku_id / uv_id / course_name。

        实测（2026-11 抓包）关键点：章节接口必须带 sign=course_sign，否则对
        「课堂型/学习空间型」课程返回空。sign 就在 /v2/api/web/classrooms/{cid}?role=5 的
        返回里（data.course_sign），形如 hibu-XXXXXXXXXXXX。
        """
        if getattr(self, "_meta", None) and self._meta.get("cid") == self.cid:
            return self._meta
        meta: Dict[str, Any] = {"cid": self.cid}
        for tpl in CANDIDATES["classroom_meta"]:
            path = tpl.format(cid=self.cid, uv_id=self.uv_id or 0)
            try:
                data = self.get_json(path)
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                LOG.debug("classroom_meta %s -> %s", path, e)
                continue
            if not isinstance(data, dict) or not data.get("data"):
                continue
            d = data["data"]
            meta.update({
                "sign": d.get("course_sign") or "",
                "course_id": str(d.get("course_id") or ""),
                "sku_id": str(d.get("free_sku_id") or d.get("sku_id") or ""),
                "uv_id": str(d.get("uv_id") or self.uv_id or ""),
                "course_name": d.get("course_name") or d.get("name") or "",
                "path": path,
            })
            self.sign = meta["sign"]
            if meta["course_id"]:
                self.course_id = meta["course_id"]
            if meta["sku_id"]:
                self.sku_id = meta["sku_id"]
            if meta["uv_id"] and not self.uv_id:
                self.uv_id = meta["uv_id"]
            LOG.debug("classroom_meta: sign=%s course_id=%s sku=%s uv=%s",
                      meta["sign"], meta["course_id"], meta["sku_id"], meta["uv_id"])
            break
        self._meta = meta
        return meta

    def chapters(self) -> List[Dict[str, Any]]:
        """拉课程树。

        混合课型的坑：mooc 接口对「课堂型 / 学习空间型」课程在没有 sign 时返回空，
        所以先取 classroom_meta 拿到 course_sign，再逐个候选试到真能解析出章节为止；
        空的响应不写缓存，否则下一门课会被上一个课的结论带偏。
        """
        if not getattr(self, "sign", ""):
            try:
                self.classroom_meta()
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                LOG.debug("取 classroom_meta 失败：%s", e)
        tried: List[str] = []
        cached = self.endpoints.get("chapters")
        order = ([cached] if cached else []) + [c for c in CANDIDATES["chapters"] if c != cached]
        last_error = None
        for tpl in order:
            if tpl in tried:
                continue
            tried.append(tpl)
            path = tpl.format(cid=self.cid, uv_id=self.uv_id or 0, sign=self.sign or "")
            try:
                data = self.get_json(path)
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                last_error = e
                LOG.debug("chapters %s -> %s", path, e)
                continue
            if not data:
                continue
            err = data.get("error_code") or data.get("errcode") if isinstance(data, dict) else None
            chapters = deep_first(data, ("course_chapter", "chapters", "chapter_list"), None)
            if isinstance(chapters, list) and chapters:
                self.endpoints["chapters"] = tpl
                if isinstance(data, dict):
                    cid_val = (data.get("data") or {}).get("course_id") if isinstance(data.get("data"), dict) else None
                    if cid_val:
                        self.course_id = str(cid_val)
                self.save_profile()
                LOG.debug("chapters 命中 %s（%d 章）", tpl, len(chapters))
                return chapters
            if isinstance(data, dict) and isinstance(data.get("data"), list) and data["data"]:
                self.endpoints["chapters"] = tpl
                self.save_profile()
                return data["data"]
            if err in (80019, 80005):
                raise RuntimeError(
                    "服务端判定未购买/未加入该课程（80019/80005）：确认网页端已加入 MOOC，且请求头 xtbz=ykt")
            LOG.debug("chapters %s 返回空结构 keys=%s", tpl,
                      list(data.keys())[:6] if isinstance(data, dict) else type(data).__name__)
        if last_error:
            LOG.debug("chapters 全部候选失败，最后一个错误：%s", last_error)
        return []

    def iter_leaves(self, chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            sections = chapter.get("section_leaf_list") or chapter.get("sections") or []
            for section in sections:
                if not isinstance(section, dict):
                    continue
                # 实测（2026-11）：section_leaf_list 里直接就是 leaf 对象，
                # 形如 {"name","chapter_id","leaf_type","id","leafinfo_id"}，没有嵌套 leaf_list
                if section.get("id") is not None and (
                        section.get("leaf_type") is not None or section.get("leafinfo_id") is not None):
                    out.append({
                        "leaf_id": str(section["id"]),
                        "name": section.get("name") or "",
                        "leaf_type": section.get("leaf_type"),
                        "chapter": chapter.get("name") or "",
                        "section": "",
                        "locked": bool(section.get("is_locked") or section.get("locked")),
                        "chapter_id": section.get("chapter_id"),
                    })
                    continue
                for leaf in section.get("leaf_list") or section.get("leafs") or []:
                    if not isinstance(leaf, dict) or leaf.get("id") is None:
                        continue
                    out.append({
                        "leaf_id": str(leaf["id"]),
                        "name": leaf.get("name") or "",
                        "leaf_type": leaf.get("leaf_type"),
                        "chapter": chapter.get("name") or "",
                        "section": section.get("name") or "",
                        "locked": bool(leaf.get("is_locked") or leaf.get("locked")),
                        "chapter_id": leaf.get("chapter_id"),
                    })
                # 有的接口把 leaf 直接挂在 chapter 下
            for leaf in chapter.get("leaf_list") or []:
                if isinstance(leaf, dict) and leaf.get("id") is not None:
                    out.append({
                        "leaf_id": str(leaf["id"]),
                        "name": leaf.get("name") or "",
                        "leaf_type": leaf.get("leaf_type"),
                        "chapter": chapter.get("name") or "",
                        "section": chapter.get("name") or "",
                        "locked": bool(leaf.get("is_locked") or leaf.get("locked")),
                        "chapter_id": leaf.get("chapter_id"),
                    })
        # 第三种：接口直接给平铺的 leaf 列表（有的课堂型课程这样）
        if not out:
            for d in iter_dicts(chapters):
                lid = d.get("leaf_id") or d.get("leafId")
                if lid is None:
                    continue
                out.append({
                    "leaf_id": str(lid),
                    "name": d.get("name") or d.get("title") or "",
                    "leaf_type": d.get("leaf_type"),
                    "chapter": d.get("chapter_name") or "",
                    "section": d.get("name") or "",
                    "locked": bool(d.get("is_locked") or d.get("locked")),
                })
        # 去重（多种挂载方式可能重复）
        seen, uniq = set(), []
        for leaf in out:
            if leaf["leaf_id"] in seen:
                continue
            seen.add(leaf["leaf_id"])
            uniq.append(leaf)
        return uniq

    def leaf_info(self, leaf_id: str) -> Dict[str, Any]:
        tpl = self.endpoints.get("leaf_info") or CANDIDATES["leaf_info"][0]
        if "leaf_info" not in self.endpoints:
            hit = self._probe("leaf_info", cid=self.cid, leaf_id=leaf_id)
            if hit:
                tpl = hit[0]
                self.endpoints["leaf_info"] = tpl
                self.save_profile()
        data = self.get_json(tpl.format(cid=self.cid, leaf_id=leaf_id))
        if not data:
            return {}
        d = data.get("data")
        return d if isinstance(d, dict) else {}

    def classify(self, leaf: Dict[str, Any], info: Dict[str, Any]) -> Dict[str, Any]:
        """不依赖 leaf_type 常量：按 leaf_info 实际内容判定类型与时长。"""
        content = info.get("content_info") if isinstance(info, dict) else {}
        media = (content.get("media") or {}) if isinstance(content, dict) else {}
        try:
            duration = float(media.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        slides = count_slide_like(info)
        if duration > 0:
            kind = "video"
        elif slides > 0:
            kind = "slide"
        elif media or (content.get("leaf_type_id") == 3 if isinstance(content, dict) else False):
            kind = "video"          # 有 media 但 duration=0：时长从进度接口的 video_length 补
        else:
            kind = "other"
        kinds = self.profile.setdefault("leaf_kinds", {})
        if leaf.get("leaf_type") is not None:
            kinds[str(leaf["leaf_type"])] = kind
        return {
            "kind": kind,
            "duration": duration,
            "slides": slides,
            "user_id": info.get("user_id"),
            "course_id": str(info.get("course_id") or ""),
            "sku_id": str(info.get("sku_id") or ""),
            "courseware_id": str(media.get("ccid") or media.get("cc_id") or ""),
            "media_name": media.get("name") or "",
            "content_keys": sorted(content.keys()) if isinstance(content, dict) else [],
        }

    # ---------------------------------------------------------- 进度
    def watched_record(self) -> Dict[str, Any]:
        tpl = self.endpoints.get("watched") or CANDIDATES["watched"][0]
        data = self.get_json(tpl, params={
            "cid": self.course_id, "user_id": self.user_id, "classroom_id": self.cid,
            "video_type": "video", "term": "latest", "uv_id": self.uv_id,
        })
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def progress(self, video_id: str) -> Dict[str, Any]:
        tpl = self.endpoints.get("progress") or CANDIDATES["progress"][0]
        if not self.course_id:
            return {}
        data = self.get_json(tpl, params={
            "cid": self.course_id, "user_id": self.user_id, "classroom_id": self.cid,
            "video_type": "video", "vtype": "rate", "video_id": video_id,
            "snapshot": 1, "term": "latest", "uv_id": self.uv_id,
        })
        if not isinstance(data, dict):
            return {}
        return (data.get("data") or {}).get(str(video_id)) or {}

    @staticmethod
    def is_completed(prog: Dict[str, Any]) -> bool:
        return bool(prog) and str(prog.get("completed")) == "1"

    @staticmethod
    def rate(prog: Dict[str, Any]) -> float:
        try:
            return float((prog or {}).get("rate") or 0)
        except (TypeError, ValueError):
            return 0.0

    # ---------------------------------------------------------- 心跳
    def heartbeat_entry(self, *, et: str, cp: float, sq: int, ts_ms: int, pg: str,
                        leaf_id: str, courseware_id: Any, api_user_id: Any,
                        duration: float, t: str = "video", slide: int = 0,
                        cards_id: int = 0) -> Dict[str, Any]:
        # 2026-11 实测：官方播放器发的是 lob=ykt、n=来源 CDN（ali-cdn.xuetangx.com）、
        # cc 用 media.ccid 那个 32 位十六进制串，d 是服务端认的时长。
        return {
            "i": 5, "et": et, "p": "web", "n": "ali-cdn.xuetangx.com", "lob": "ykt",
            "cp": round(float(cp), 1), "fp": 0, "tp": 0, "sp": 1,
            "ts": str(ts_ms), "u": api_user_id, "uip": "",
            "c": str(self.course_id), "v": str(leaf_id), "skuid": str(self.sku_id),
            "classroomid": str(self.cid), "cc": str(courseware_id),
            "d": float(duration), "pg": pg, "sq": sq, "t": t,
            "cards_id": cards_id, "slide": slide, "v_url": "",
        }

    def mark_study_leaf(self, leaf_id: Any) -> bool:
        """打开课件时官方会打一枪 add_study_leaf；带上它更接近真实行为。

        有些课程这个接口恒返回 ok:false（实测 2026-11），一旦发现就本进程内不再浪费请求。
        """
        if getattr(self, "_mark_leaf_ok", True) is False:
            return False
        try:
            data = self.post_json("/c27/online_courseware/xty/instance/add_study_leaf/",
                                  {"leaf_id": leaf_id, "classroom_id": self.cid})
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            LOG.debug("add_study_leaf 失败：%s", e)
            return False
        payload = data.get("data") if isinstance(data, dict) else None
        ok = bool(isinstance(data, dict) and data.get("success") is True
                  and (payload != {"ok": False}))
        if not ok:
            self._mark_leaf_ok = False
            LOG.debug("add_study_leaf 本课程恒 ok:false，之后跳过该请求")
        return ok

    def send_heartbeat(self, entries: List[Dict[str, Any]]) -> bool:
        tpl = self.endpoints.get("heartbeat") or CANDIDATES["heartbeat"][0]
        if "heartbeat" not in self.endpoints:
            self.endpoints["heartbeat"] = tpl
            self.save_profile()
        try:
            status, _ = self.request("POST", tpl, body={"heart_data": entries}, timeout=30)
        except AuthError:
            raise
        except TransientError as e:
            LOG.warning("心跳上报异常：%s", e)
            return False
        return status == 200

    def activity_leaves(self) -> List[Dict[str, Any]]:
        """课堂型课程兜底：用活动日志里 type=17（课件/视频）记录构造 leaf 列表。

        有些校内课没有 leaf 章节，但学习日志里每个课件都记了 leaf_id + courseware_id，
        拿它们套 leaf_info 有时也能查到时长，从而照常按视频刷。
        """
        if not self.cid:
            return []
        tpl = self.endpoints.get("activities") or CANDIDATES["activities"][0]
        try:
            data = self.get_json(tpl.format(cid=self.cid)) or {}
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            LOG.debug("活动日志拉取失败：%s", e)
            return []
        acts = ((data.get("data") or {}).get("activities") or []) if isinstance(data, dict) else []
        out: List[Dict[str, Any]] = []
        seen = set()
        for a in acts:
            if not isinstance(a, dict):
                continue
            content = a.get("content") or {}
            lid = content.get("leaf_id") or content.get("leafId")
            if not lid or str(lid) in seen:
                continue
            seen.add(str(lid))
            name = content.get("name") or content.get("title") or f"activity-{lid}"
            out.append({
                "leaf_id": str(lid),
                "name": str(name),
                "leaf_type": a.get("type"),
                "chapter": "活动日志",
                "section": str(content.get("section_name") or ""),
                "locked": False,
            })
            if a.get("courseware_id"):
                self._courseware[str(lid)] = str(a["courseware_id"])
        return out

    def courseware_map(self) -> Dict[str, str]:
        """从活动日志取 leaf_id -> courseware_id（心跳 cc 字段）。"""
        tpl = self.endpoints.get("activities") or CANDIDATES["activities"][0]
        mapping: Dict[str, str] = {}
        if not self.cid:
            self._courseware = {}
            return {}
        try:
            data = self.get_json(tpl.format(cid=self.cid))
            self.endpoints["activities"] = tpl
            acts = ((data or {}).get("data") or {}).get("activities") or []
            for act in acts:
                if not isinstance(act, dict) or act.get("type") != 17:
                    continue
                leaf_id = (act.get("content") or {}).get("leaf_id")
                cw = act.get("courseware_id")
                if leaf_id and cw:
                    mapping[str(leaf_id)] = str(cw)
            LOG.info("活动日志：%d 个课件 ID 映射", len(mapping))
        except Exception as e:  # noqa: BLE001
            LOG.warning("课件映射失败（cc 回落到 leaf_id）：%s", e)
        self._courseware = mapping
        return mapping
