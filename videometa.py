# -*- coding: utf-8 -*-
"""视频时长兜底：leaf_info / 进度接口都拿不到时长时，直接读 mp4 的 moov->mvhd。

背景（2026-11 实测）：
  · leaf_info.data.content_info.media.duration 经常是 0
  · /video-log/get_video_watch_progress/ 的 video_length 只有"看过"的视频才有
没看过的视频两处都是 0，就会卡在"拿不到时长无法刷课"。

办法：先取播放地址（/api/open/audiovideo/playurl），再对 mp4 做 Range 请求，
解析 ISO BMFF 的 mvhd（timescale + duration），纯标准库，不依赖 ffprobe。

    from videometa import duration_from_playurl
    dur = duration_from_playurl(client, "FAE4CBC39AA3F1EEF3342D97BB1D6DF8")
"""
from __future__ import annotations

import logging
import struct
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

LOG = logging.getLogger("videometa")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PLAYURL_CANDIDATES = [
    "/api/open/audiovideo/playurl?video_id={ccid}&provider=cc&file_type=1&is_single=0",
    "/api/open/audiovideo/playurl?video_id={ccid}&provider=cc&is_single=0",
    "/api/open/audiovideo/playurl?video_id={ccid}&provider=cc&file_type=2&is_single=0",
]

HEAD_SIZE = 2048


def _range(url: str, start: int, end: Optional[int] = None, timeout: float = 20.0) -> bytes:
    rng = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Range": rng, "Accept": "*/*", "Referer": "https://changjiang.yuketang.cn/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _read_head(url: str, n: int = HEAD_SIZE) -> Tuple[bytes, Optional[int]]:
    """读前 n 字节，同时拿到总大小（Content-Range）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Range": f"bytes=0-{n - 1}", "Accept": "*/*",
        "Referer": "https://changjiang.yuketang.cn/",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
        total = None
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            try:
                total = int(cr.split("/")[-1])
            except ValueError:
                total = None
        return data, total


def _boxes(buf: bytes, offset: int = 0, end: Optional[int] = None):
    """遍历 buf 里的 box，产出 (type, start_of_body, body_len)。只处理完整 box。"""
    end = len(buf) if end is None else end
    pos = offset
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        typ = buf[pos + 4:pos + 8]
        header = 8
        if size == 1:  # 64 位长度
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:  # 到文件尾
            size = end - pos
        if size < header or pos + size > end:
            return
        yield typ, pos + header, size - header
        pos += size


def _parse_mvhd(buf: bytes, body_start: int, body_len: int) -> Optional[float]:
    body = buf[body_start:body_start + body_len]
    if len(body) < 20:
        return None
    version = body[0]
    if version == 1:
        if len(body) < 32:
            return None
        timescale = struct.unpack(">I", body[20:24])[0]
        duration = struct.unpack(">Q", body[24:32])[0]
    else:
        timescale = struct.unpack(">I", body[12:16])[0]
        duration = struct.unpack(">I", body[16:20])[0]
    if not timescale:
        return None
    return duration / float(timescale)


def _size(url: str, timeout: float = 20.0) -> Optional[int]:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Range": "bytes=0-0", "Accept": "*/*",
        "Referer": "https://changjiang.yuketang.cn/",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cr = r.headers.get("Content-Range") or ""
            if "/" in cr:
                return int(cr.split("/")[-1])
            cl = r.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception as e:  # noqa: BLE001
        LOG.debug("取文件大小失败：%s", e)
        return None


def _read_at(url: str, pos: int, n: int) -> bytes:
    return _range(url, pos, pos + n - 1)


def _moov_duration(moov: bytes) -> Optional[float]:
    for typ, body_start, body_len in _boxes(moov):
        if typ == b"mvhd":
            return _parse_mvhd(moov, body_start, body_len)
    return None


def mp4_duration(url: str) -> Optional[float]:
    """流式走顶层 box，遇到 moov 只读它开头几 KB（mvhd 通常是第一个子 box）。

    moov 在文件尾部（没有 faststart）时，再读尾部 1MB 扫一遍。
    """
    total = _size(url)
    pos = 0
    for _ in range(128):
        hdr = _read_at(url, pos, 16)
        if len(hdr) < 8:
            break
        size = struct.unpack(">I", hdr[:4])[0]
        typ = hdr[4:8]
        header = 8
        if size == 1:
            if len(hdr) < 16:
                break
            size = struct.unpack(">Q", hdr[8:16])[0]
            header = 16
        elif size == 0:
            size = (total - pos) if total else 0
        if size < header:
            break
        LOG.debug("box %s at %d size=%d", typ, pos, size)
        if typ == b"moov":
            body_len = min(size - header, 16384)
            body = _read_at(url, pos + header, body_len)
            dur = _moov_duration(body)
            if dur:
                return dur
        pos += size
        if total and pos >= total:
            break

    # moov 在尾部
    if total and total > 1024:
        tail_start = max(0, total - 1024 * 1024)
        try:
            tail = _range(url, tail_start, total - 1)
        except Exception as e:  # noqa: BLE001
            LOG.debug("读尾部失败：%s", e)
            return None
        idx = tail.find(b"moov")
        while idx != -1:
            if idx >= 4:
                start = idx - 4
                size = struct.unpack(">I", tail[start:start + 4])[0]
                body = tail[start + 8:start + 8 + min(max(size - 8, 0), 16384)]
                dur = _moov_duration(body)
                if dur:
                    return dur
            idx = tail.find(b"moov", idx + 1)
    return None


def playurl(client: Any, ccid: str) -> Optional[str]:
    """向雨课堂要播放地址，返回第一个 http 地址。"""
    for tpl in PLAYURL_CANDIDATES:
        path = tpl.format(ccid=ccid)
        try:
            data = client.get_json(path)
        except Exception as e:  # noqa: BLE001
            LOG.debug("playurl %s 失败：%s", path, e)
            continue
        url = _pick_source(data)
        if url:
            return url
    return None


def _pick_source(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    sources = (((data.get("data") or {}).get("playurl") or {}).get("sources") or {})
    if isinstance(sources, dict):
        for quality in ("quality20", "quality10", "quality30", "quality3", "quality4"):
            arr = sources.get(quality)
            if isinstance(arr, list) and arr:
                return str(arr[0])
        for arr in sources.values():
            if isinstance(arr, list) and arr:
                return str(arr[0])
    # 有的返回直接给 url
    for key in ("url", "playurl"):
        val = (data.get("data") or {}).get(key) if isinstance(data.get("data"), dict) else None
        if isinstance(val, str) and val.startswith("http"):
            return val
    return None


def duration_from_playurl(client: Any, ccid: str) -> Optional[float]:
    if not ccid:
        return None
    url = playurl(client, ccid)
    if not url:
        LOG.debug("ccid=%s 没有播放地址", ccid)
        return None
    LOG.debug("ccid=%s 播放地址 %s", ccid, url[:90])
    dur = mp4_duration(url)
    if dur and dur > 0:
        LOG.info("ccid=%s 从 mp4 头解析出时长 %.0fs", ccid, dur)
        return dur
    return None


if __name__ == "__main__":  # 手工验证：python videometa.py <mp4_url>
    import sys

    if len(sys.argv) > 1:
        print(mp4_duration(sys.argv[1]))
