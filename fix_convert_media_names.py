#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 IncandescenceArchiver 旧版 `convert` 导入媒体的错误文件名。

依据 archive.py 原版：
  旧 convert: safe_filename(ts, url, ext) — URL 点号/斜线等变 _，前 100 字符
  fetch-media: netloc + path，经 [^\\w\\-_.] 清洗，前 120 字符；依据 URL 决定扩展名

普通媒体通过 `wayback_snapshots/json/*.json` 的原始 URL 确认；
额外识别 preview_image_repair 生成的 pbs.twimg.com 视频缩略图：
优先使用原始 JSON 或 preview_image_repair_logs 的 URL；
没有 URL 证据时仅对格式明确、未截断的标准缩略图路径做保守还原。
对不能可靠追溯的文件只报告，不自动更名。

用法（脚本放在 archive.py 旁边，终端进入账号目录）：
  python ../../fix_convert_media_names.py                    # 仅扫描/生成报告
  python ../../fix_convert_media_names.py --apply            # 真正移动/更名并修复 HTML/index 引用
  python ../../fix_convert_media_names.py --delete-trash     # 校验通过后直接删除回收文件夹
  python ../../fix_convert_media_names.py --delete-backups   # 删除 HTML/index 原版备份
  python ../../fix_convert_media_names.py --runall           # 扫描 → 修复 → 清理，一次完成
  python ../../fix_convert_media_names.py --review-issues    # 可点击定位到资源管理器的问题清单
  python ../../fix_convert_media_names.py --resolve-conflicts # 单独处理可确认对应关系的 CONFLICT，文件体积择大
  python ../../fix_convert_media_names.py --root <账号目录> --apply

Python >= 3.10，标准库，无第三方依赖。
普通 --apply：同源图片 SHA-256 不同时按字节数择大；等体积不同哈希保持 CONFLICT。
--resolve-conflicts：仅处理普通规则留下的冲突，图片与视频按字节数择大；
等体积不同哈希且正确命名版本存在时保留正确命名文件，其余先回收。
"""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime
import hashlib
import html
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import sys
import time
from urllib.parse import urlparse
from collections import defaultdict, Counter

RECYCLER = "_convert_filename_recycle"
REPORTS = "_convert_filename_reports"
BACKUPS = "_convert_filename_reference_backups"
MANIFEST = "_manifest.jsonl"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
MEDIA_EXTS = IMAGE_EXTS + (".mp4", ".mov", ".m4v", ".webm", ".ts")
TS_RE = re.compile(r"^(\d{14})_(.+)$")
_IMG_NEW_ID = re.compile(r"_media_([A-Za-z0-9_-]+?)_(?:jpg|jpeg|png|gif|webp)\.(?:jpg|jpeg|png|gif|webp)$", re.I)
_IMG_OLD_ID = re.compile(r"_media_([A-Za-z0-9_-]+?)\.(?:jpg|jpeg|png|gif|webp)$", re.I)
_VIDEO_ID = re.compile(r"(?:amplify_video|ext_tw_video)[/_](\d+)|tweet_video[/_]([A-Za-z0-9_-]+)", re.I)


def media_identity(kind: str, filename: str) -> str:
    if kind == "image":
        m = _IMG_NEW_ID.search(filename) or _IMG_OLD_ID.search(filename)
        return m.group(1) if m else ""
    m = _VIDEO_ID.search(filename)
    return (m.group(1) or m.group(2)) if m else ""
OLD_HINT = re.compile(r"^\d{14}_(?:pbs_twimg_com_|video_twimg_com_)", re.I)
THUMB_OLD_HINT = re.compile(
    r"^\d{14}_pbs_twimg_com_(?:amplify_video_thumb|ext_tw_video_thumb)_", re.I
)
# 仅用于没有 URL/日志证据时的安全兜底：Twitter CDN 缩略图的典型完整路径。
# 转换前缀和最后一个扩展名中的点号即可；中间的 '_' 均为原始字符或斜线的清洗结果。
THUMB_COMPLETE = re.compile(
    r"^(?P<ts>\d{14})_pbs_twimg_com_"
    r"(?P<group>amplify_video_thumb|ext_tw_video_thumb)_"
    r"(?P<video_id>\d+)_(?P<location>pu_img|img)_"
    r"(?P<token>[A-Za-z0-9_-]+)_(?P<url_ext>jpg|jpeg|png|gif|webp)"
    r"\.(?P<stored_ext>jpg|jpeg|png|gif|webp)$", re.I
)


def is_thumbnail_old(name: str) -> bool:
    return bool(THUMB_OLD_HINT.match(name))


def get_status(record: dict) -> str:
    return record["status"].removeprefix("THUMB_")


def set_status(record: dict, status: str) -> None:
    record["status"] = ("THUMB_" if record.get("thumbnail") else "") + status


def infer_complete_thumbnail(old_filename: str) -> tuple[str, str] | None:
    """仅还原符合固定路径结构且绝无 100 字符截断风险的完整缩略图。

    返回 (原始 URL 候选, 正确文件名)。URL 只是路径结构的保守重建，
    并非原始数据证明；特殊路径、长名称或无法确认扩展名的文件一律跳过。
    """
    m = THUMB_COMPLETE.fullmatch(old_filename)
    if not m:
        return None
    d = m.groupdict()
    path = (f"/{d['group']}/{d['video_id']}/"
            f"{d['location'].replace('_', '/')}/{d['token']}.{d['url_ext']}")
    url = "https://pbs.twimg.com" + path
    # 如果旧版 100 字符裁剪曾触发，即使表面上有 _jpg 结尾也不能确认完整路径。
    clean = re.sub(r"[^\w\-_]", "_", "pbs.twimg.com" + path)
    if len(clean) >= 100:
        return None
    ts = d["ts"]
    stored_ext = "." + d["stored_ext"]
    if old_name(ts, url, stored_ext) != old_filename:
        return None
    new_filename = new_name(ts, url, "image")
    if new_filename == old_filename:
        return None
    return url, new_filename


def utc_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sha256(path: Path, progress: DeleteProgress | None = None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024 * 4), b""):
            h.update(chunk)
            if progress:
                progress.pulse(path.name)
    return h.hexdigest()



def image_dimensions(path: Path) -> tuple[int, int] | None:
    """用标准库读取 JPEG/PNG/GIF/WebP 尺寸，不解码完整图像。

    无法识别格式或尺寸异常时返回 None，交由 CONFLICT 保守处理。
    """
    try:
        with path.open("rb") as f:
            head = f.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
            elif head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
            elif head.startswith(b"\xff\xd8"):
                f.seek(2)
                w = h = 0
                sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
                while True:
                    b = f.read(1)
                    if not b:
                        break
                    if b != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if not marker:
                        break
                    code = marker[0]
                    if code in (0xD9, 0xDA):
                        break
                    if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
                        continue
                    length_raw = f.read(2)
                    if len(length_raw) != 2:
                        break
                    length = int.from_bytes(length_raw, "big")
                    if length < 2:
                        break
                    if code in sof and length >= 7:
                        data = f.read(5)
                        if len(data) == 5:
                            h = int.from_bytes(data[1:3], "big")
                            w = int.from_bytes(data[3:5], "big")
                        break
                    f.seek(length - 2, 1)
            elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                typ = head[12:16]
                if typ == b"VP8X" and len(head) >= 30:
                    w = int.from_bytes(head[24:27], "little") + 1
                    h = int.from_bytes(head[27:30], "little") + 1
                elif typ == b"VP8L" and len(head) >= 25 and head[20] == 0x2F:
                    b1, b2, b3, b4 = head[21:25]
                    w = 1 + b1 + ((b2 & 0x3F) << 8)
                    h = 1 + (b2 >> 6) + (b3 << 2) + ((b4 & 0x0F) << 10)
                elif typ == b"VP8 " and len(head) >= 30 and head[23:26] == b"\x9d\x01\x2a":
                    w = int.from_bytes(head[26:28], "little") & 0x3FFF
                    h = int.from_bytes(head[28:30], "little") & 0x3FFF
                else:
                    return None
            else:
                return None
    except (OSError, ValueError, struct.error):
        return None
    return (w, h) if 0 < w <= 100000 and 0 < h <= 100000 else None


def same_aspect_ratio(dimensions: list[tuple[int, int]]) -> bool:
    if not dimensions:
        return False
    w0, h0 = dimensions[0]
    return all(w * h0 == w0 * h for w, h in dimensions[1:])


def describe_dims(path: Path) -> str:
    dim = image_dimensions(path)
    return f"{dim[0]}x{dim[1]}" if dim else "未知尺寸"


def manifest_entry(kind: str, old: str, new: str, recycled: Path,
                   retained: Path, policy: str) -> dict:
    return dict(kind=kind, old=old, new=new, sha256=sha256(recycled),
                kept_sha256=sha256(retained), policy=policy)


def old_name(ts: str, url: str, ext: str) -> str:
    no_query = url.split("?")[0]
    clean = re.sub(r"[^\w\-_]", "_", no_query.replace("https://", "").replace("http://", ""))
    return f"{ts}_{clean[:100]}{ext}"


def ext_from_url(url: str) -> str:
    path = url.split("?")[0].lower()
    for e in (".png", ".gif", ".webp", ".jpeg", ".jpg"):
        if path.endswith(e):
            return e
    return ".jpg"


def new_name(ts: str, url: str, kind: str) -> str:
    parsed = urlparse(url)
    stem = re.sub(r"[^\w\-_.]", "_", (parsed.netloc + parsed.path).strip("/"))[:120]
    if kind == "video":
        if not stem.lower().endswith(".mp4"):
            stem += ".mp4"
    elif not stem.lower().endswith(IMAGE_EXTS):
        stem += ext_from_url(url)
    return f"{ts}_{stem}"


def media_kind(url: str) -> str:
    if "profile_images" in url:
        return "avatar"
    if any(x in url for x in ("amplify_video_thumb", "ext_tw_video_thumb", "tweet_video_thumb")):
        return "thumbnail"
    if ("video.twimg.com" in url or "ext_tw_video" in url or
            "amplify_video/" in url or url.lower().endswith(".mp4")):
        return "video"
    return "image"


def url_sources(data: dict):
    inc = data.get("includes") or {}
    if not isinstance(inc, dict):
        return
    for m in inc.get("media") or []:
        if not isinstance(m, dict):
            continue
        typ = m.get("type", "")
        if typ == "photo" and isinstance(m.get("url"), str):
            yield m["url"]
        elif typ in ("video", "animated_gif"):
            for v in m.get("variants") or []:
                if isinstance(v, dict) and v.get("content_type") == "video/mp4" and isinstance(v.get("url"), str):
                    yield v["url"]
        # 原版 convert 会纳入 preview_image_url；缩略图的特殊 URL 被跳过
        if isinstance(m.get("preview_image_url"), str):
            yield m["preview_image_url"]


def load_urls(json_dir: Path) -> tuple[dict[str, set[str]], int, int]:
    """不同时间戳前缀可能对应同一 URL；因此按旧命名的非时间戳部分建立候选。"""
    result: dict[str, set[str]] = defaultdict(set)
    valid = bad = 0
    for f in sorted(json_dir.glob("*.json")) if json_dir.is_dir() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("JSON 顶层不是对象")
        except (OSError, ValueError, UnicodeError) as e:
            bad += 1
            print(f"  [JSON 无法读取] {f.name}: {e}")
            continue
        valid += 1
        for url in url_sources(data):
            if url.startswith(("http://", "https://")):
                k = media_kind(url)
                if k in ("image", "video", "thumbnail"):
                    result[k].add(url)
    return result, valid, bad


def load_thumbnail_log_urls(snap: Path) -> tuple[set[str], int]:
    """从 preview_image_repair 的已保存日志中提取真实缩略图 URL。

    不依赖用户仍保存 downloader 的源 dump；只读本账号的独立日志。
    日志是历史记录，依旧需要精确重演旧版文件名才能认领本地文件。
    """
    log_dir = snap / "preview_image_repair_logs"
    if not log_dir.is_dir() or log_dir.is_symlink():
        return set(), 0
    urls: set[str] = set()
    scanned = 0
    for path in sorted(log_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file() or not path.name.startswith(
            ("scan_", "download_plan_", "download_results_", "convert_results_")
        ):
            continue
        scanned += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError) as exc:
            print(f"  [缩略图日志读取失败] {path.name}: {exc}")
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("missing", "items", "results"):
            entries = payload.get(key)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                if (isinstance(url, str) and url.startswith(("https://", "http://"))
                        and media_kind(url) == "thumbnail"
                        and urlparse(url).hostname == "pbs.twimg.com"):
                    urls.add(url)
    return urls, scanned


def discover(root: Path):
    """仅用 URL 可重构原版 safe_filename 的候选；精确命中才判错。"""
    snap = root / "wayback_snapshots" if (root / "wayback_snapshots").is_dir() else root
    if not (snap / "image").is_dir() and not (snap / "video").is_dir():
        raise RuntimeError(f"找不到 image/ 或 video/：{snap}")
    urls_by_kind, n_json, n_bad_json = load_urls(snap / "json")
    log_urls, n_thumb_logs = load_thumbnail_log_urls(snap)
    urls_by_kind["thumbnail"].update(log_urls)
    if n_thumb_logs:
        print(f"[缩略图日志] 已读取 {n_thumb_logs} 个 JSON 日志，提取 {len(log_urls)} 个缩略图 URL")
    found: dict[tuple[str, str], dict] = {}
    suspicious = []
    total_media = 0
    for kind in ("image", "video"):
        folder = snap / kind
        if not folder.is_dir():
            continue
        names = {p.name: p for p in folder.iterdir() if p.is_file() and not p.is_symlink()}
        total_media += len(names)
        # 根据真实文件的扩展名重演旧 convert。旧版使用 hash_path 扩展名，
        # 无法从媒体 URL 猜出扩展名，因此对现存扩展名逐一精确匹配。
        by_ext = defaultdict(set)
        for n in names:
            suffix = Path(n).suffix.lower()
            if suffix in MEDIA_EXTS:
                by_ext[suffix].add(n)
        possible: dict[str, set[tuple[str, str]]] = defaultdict(set)
        # 重新构造按后缀映射，支持任何快照时间戳。
        by_tail: dict[tuple[str, str], list[str]] = defaultdict(list)
        for n in names:
            m = TS_RE.match(n)
            if m:
                by_tail[(m.group(2), Path(n).suffix.lower())].append(n)
        candidate_urls = set(urls_by_kind[kind])
        if kind == "image":
            candidate_urls.update(urls_by_kind["thumbnail"])
        for url in candidate_urls:
            thumbnail = kind == "image" and media_kind(url) == "thumbnail"
            for ext in by_ext:
                tail = old_name("00000000000000", url, ext)[15:]
                for n in by_tail.get((tail, ext), []):
                    ts = n[:14]
                    target = new_name(ts, url, kind)
                    if target != n:
                        possible[n].add((url, target))

        # 对没有任何原始 URL 匹配的标准缩略图做保守的完整路径还原。
        # 即使没有推文 JSON 的原始引用，也不应将这类文件全部留在 UNVERIFIED。
        if kind == "image":
            for fname in names:
                if not is_thumbnail_old(fname) or fname in possible:
                    continue
                inferred = infer_complete_thumbnail(fname)
                if inferred:
                    possible[fname].add(inferred)

        for fname, options in sorted(possible.items()):
            thumbnail = kind == "image" and is_thumbnail_old(fname)
            targets = {t for _, t in options}
            path = names[fname]
            if len(targets) != 1:
                found[(kind, fname)] = dict(kind=kind, old=fname, new="", status="THUMB_AMBIGUOUS" if thumbnail else "AMBIGUOUS", reason="相同旧文件名可映射到多个正确文件名", url="", thumbnail=thumbnail)
                continue
            target_name = next(iter(targets))
            src_url = sorted(u for u, _ in options)[0]
            if not target_name or "/" in target_name or "\\" in target_name:
                found[(kind, fname)] = dict(kind=kind, old=fname, new="", status="THUMB_AMBIGUOUS" if thumbnail else "AMBIGUOUS", reason="目标文件名不安全", url=src_url, thumbnail=thumbnail)
                continue
            # 源 URL 来自真实 JSON/日志，还是由完整的标准路径结构还原。
            evidence = ("源自 JSON / preview_image_repair 日志，旧文件名精确匹配"
                        if src_url in candidate_urls else "标准缩略图完整路径可还原；旧版命名重演一致")
            found[(kind, fname)] = dict(kind=kind, old=fname, new=target_name,
                                        status="THUMB_PENDING" if thumbnail else "PENDING",
                                        reason=evidence if thumbnail else "", url=src_url,
                                        thumbnail=thumbnail)
        # 额外的安全兜底：JSON 没有原始 URL 时，如果同一个时间戳下已经有
        # 唯一的正确命名版本，且媒体身份相同，则允许交由 SHA-256 检查决定。
        # 不据此猜测目标文件名；没有对应正确版本仍然只报告。
        good_by_identity: dict[tuple[str, str], list[str]] = defaultdict(list)
        for name in names:
            m = TS_RE.match(name)
            if m and ((kind == "image" and "_pbs.twimg.com_" in name) or
                      (kind == "video" and "_video.twimg.com_" in name)):
                identity = media_identity(kind, name)
                if identity:
                    good_by_identity[(m.group(1), identity)].append(name)
        for fname in sorted(names):
            if not OLD_HINT.match(fname) or (kind, fname) in found:
                continue
            identity = media_identity(kind, fname)
            m = TS_RE.match(fname)
            good = good_by_identity.get((m.group(1), identity), []) if m and identity else []
            if len(good) == 1 and good[0] != fname:
                thumbnail = kind == "image" and is_thumbnail_old(fname)
                found[(kind, fname)] = dict(kind=kind, old=fname, new=good[0],
                                            status="THUMB_PENDING" if thumbnail else "PENDING", reason="通过相同时间戳和媒体 ID 匹配本地正确版本，仍需哈希一致",
                                            url="", thumbnail=thumbnail, id_only=True)
            else:
                thumbnail = kind == "image" and is_thumbnail_old(fname)
                reason = ("缩略图 URL 未在 JSON 或 preview_image_repair 日志中找到，"
                          "且文件名不能安全还原为完整的标准缩略图路径；保留不动"
                          if thumbnail else
                          "看起来是旧 convert 命名，但 JSON 中无法找到唯一的原始 URL；保留不动")
                suspicious.append(dict(kind=kind, old=fname, new="",
                                       status="THUMB_UNVERIFIED" if thumbnail else "UNVERIFIED",
                                       reason=reason, url="", thumbnail=thumbnail))
    return snap, found, suspicious, total_media, n_json, n_bad_json


def classify(snap: Path, found: dict, resolve_conflicts: bool = False):
    """普通模式保留视频和等体积冲突；冲突处理模式按字节数处理已匹配的 CONFLICT。"""
    groups = defaultdict(list)
    for rec in found.values():
        if get_status(rec) == "PENDING":
            groups[(rec["kind"], rec["new"])].append(rec)

    for (kind, target_name), members in groups.items():
        folder = snap / kind
        target = folder / target_name

        def conflict(reason: str):
            for r in members:
                set_status(r, "CONFLICT")
                r["reason"] = reason

        if target.is_symlink():
            conflict("目标文件是符号链接")
            continue
        sources = [folder / r["old"] for r in members]
        candidates = list(zip(members, sources))
        if target.exists():
            if not target.is_file():
                conflict("目标路径已经存在且不是文件")
                continue
            candidates.append((None, target))
        try:
            hashes = {p: sha256(p) for _, p in candidates}
        except OSError as exc:
            conflict(f"无法计算 SHA-256：{exc}")
            continue
        # 用来阻止分类和操作之间文件被外部程序改写。
        for r, path in candidates:
            if r is not None:
                r["source_sha256"] = hashes[path]
        if target.exists():
            for r in members:
                r["target_sha256_before"] = hashes[target]

        if len(set(hashes.values())) == 1:
            if target.exists():
                for r in members:
                    set_status(r, "TRASH")
                    r["policy"] = "exact"
                    r["reason"] = "正确文件已存在且 SHA-256 一致"
            else:
                first = sorted(members, key=lambda r: r["old"])[0]
                set_status(first, "RENAME")
                first["reason"] = "正确文件尚不存在"
                for r in members:
                    if r is not first:
                        set_status(r, "TRASH")
                        r["policy"] = "exact"
                        r["reason"] = "多份旧文件目标相同且 SHA-256 一致；保留一份改名"
            continue

        if not resolve_conflicts and (kind != "image" or any(r.get("id_only") for r in members)):
            conflict("SHA-256 不一致；视频或仅媒体 ID 匹配的文件不自动按尺寸合并")
            continue
        # 只按文件体积（字节数）择优，不比较宽高比。冲突专用模式也允许视频。
        # 文件体积不能证明内容一致，因此小体积版本先回收供人工确认。
        try:
            sizes = {p: p.stat().st_size for _, p in candidates}
        except OSError as exc:
            conflict(f"无法读取文件大小：{exc}")
            continue
        max_bytes = max(sizes.values())
        best = [(r, p) for r, p in candidates if sizes[p] == max_bytes]
        if len({hashes[p] for _, p in best}) != 1:
            # 只有冲突专用模式、且正确命名版本存在并且也是最大体积时，
            # 才允许等体积但哈希不同的文件择正确名称保留。
            if not (resolve_conflicts and target.exists() and any(p == target for _, p in best)):
                conflict("最大文件体积并列但 SHA-256 不同，且没有可优先保留的正确命名版本；" +
                         "；".join(f"{p.name}={sizes[p]} B" for _, p in candidates))
                continue
        # 体积最大的版本不止一个但内容完全一致时，优先保留正确命名版本。
        winner_rec, winner = sorted(best, key=lambda item: (item[0] is not None, item[1].name))[0]
        if target.exists() and any(p == target for _, p in best):
            winner_rec, winner = None, target
        report_sizes = (f"保留 {winner.name} ({sizes[winner]} B)；" +
                        "；".join(f"{p.name}: {sizes[p]} B" for _, p in candidates))

        for r, p in candidates:
            if r is None:
                continue
            if r is winner_rec:
                if target.exists():
                    set_status(r, "REPLACE")
                    r["displaced_name"] = "__convertfix_replaced__" + target_name
                    r["reason"] = "错误命名版本文件体积更大，替换正确命名的小文件；" + report_sizes
                else:
                    set_status(r, "RENAME")
                    r["reason"] = "多版本文件体积不同，选择更大文件更名；" + report_sizes
            else:
                set_status(r, "TRASH")
                r["policy"] = ("exact" if hashes[p] == hashes[winner] else
                               "filesize_equal" if sizes[p] == sizes[winner] else "filesize")
                r["reason"] = "按文件体积保留较大版本，其余移至回收文件夹；" + report_sizes


def replacement_regex(mapping: dict[str, str]):
    if not mapping:
        return None
    keys = sorted((re.escape(k) for k in mapping), key=len, reverse=True)
    # 只匹配完整 ../image/文件名 或 ../video/文件名，避免前缀碰撞。
    return re.compile(r"(?<![A-Za-z0-9_.-])(?:" + "|".join(keys) + r")(?![A-Za-z0-9_.-])")


def reference_files(snap: Path):
    folder = snap / "html"
    if folder.is_symlink():
        raise RuntimeError(f"HTML 目录是符号链接：{folder}")
    if folder.is_dir():
        for p in sorted(folder.glob("*.html")):
            yield p
    index = snap / "index.json"
    if index.exists() or index.is_symlink():
        yield index


def prepare_reference_updates(snap: Path, mapping: dict[str, str]):
    """在更改媒体之前检查所有待修改的 HTML/index；任何失败则中止整个操作。"""
    pat = replacement_regex(mapping)
    if not pat:
        return []
    updates = []
    for p in reference_files(snap):
        if p.is_symlink() or not p.is_file():
            raise RuntimeError(f"引用文件不是普通文件：{p}")
        raw = p.read_bytes()
        txt = raw.decode("utf-8-sig")
        replaced, n = pat.subn(lambda m: mapping[m.group(0)], txt)
        if not n:
            continue
        encoded = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + replaced.encode("utf-8")
        if p.suffix.lower() == ".json":
            json.loads(replaced)  # 不允许写坏 index.json
        updates.append((p, raw, encoded))
    return updates


def atomic_write_bytes(path: Path, data: bytes):
    """同目录临时文件 + replace，避免中途写入留下半个 HTML/index。"""
    import tempfile
    temp = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".convertfix_", suffix=".tmp", dir=path.parent,
                                         delete=False) as f:
            temp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copymode(path, temp)
        os.replace(temp, path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def save_reference_backups(snap: Path, updates, backup_root: Path):
    """先把所有引用备份保存完，之后才允许触碰媒体。"""
    for p, raw, _ in updates:
        relative = p.relative_to(snap)
        backup = backup_root / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())


def explorer_token(snap: Path, file_path: Path) -> str:
    """Report locator contains a path relative to the archive, not executable user input."""
    rel = file_path.relative_to(snap)
    return base64.urlsafe_b64encode(str(rel).encode("utf-8")).decode("ascii")


def locate_in_explorer(snap: Path, token: str) -> int:
    """Called by report shortcut. Explorer must select the file, not open it."""
    if os.name != "nt":
        print("[定位失败] 这个功能需要 Windows 资源管理器。")
        return 2
    try:
        rel = Path(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
        if (rel.is_absolute() or len(rel.parts) != 2
                or rel.parts[0] not in ("image", "video")
                or rel.parts[1] in ("", ".", "..")
                or Path(rel.parts[1]).name != rel.parts[1]):
            raise ValueError("非法媒体路径")
        candidate = snap / rel
        if candidate.is_symlink():
            raise ValueError("不允许定位符号链接")
        file_path = candidate.resolve(strict=True)
        if not file_path.is_file() or file_path.parent != (snap / rel.parts[0]).resolve():
            raise ValueError("不是有效的存档媒体文件")
        subprocess.Popen(["explorer.exe", "/select,", str(file_path)])
        return 0
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"[定位失败] 文件可能已经移动或删除：{exc}")
        return 2


def review_issues(snap: Path) -> int:
    """Serve interactive report on loopback. Click selects file in Explorer; no .cmd/file:// links."""
    if os.name != "nt":
        print("[无法打开] 交互定位需要 Windows。")
        return 2
    folder = snap / REPORTS
    reports = sorted(
        list(folder.glob("apply_issues_*.csv")) + list(folder.glob("resolve_conflicts_issues_*.csv")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not reports:
        print("[没有报告] 请先执行 --apply 或 --resolve-conflicts 生成待检查 CSV。")
        return 2
    with reports[0].open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    token = secrets.token_urlsafe(24)

    def current_file(idx: int, side: str) -> Path | None:
        if idx < 0 or idx >= len(rows) or side not in ("old", "new"):
            return None
        r = rows[idx]
        kind, name = r.get("kind", ""), r.get(side, "")
        if kind not in ("image", "video") or not name or name in (".", ".."):
            return None
        if Path(name).name != name or any(c in name for c in "\\/\x00"):
            return None
        directory = (snap / kind).resolve()
        path = snap / kind / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != directory:
            return None
        return path.resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            if parse_qs(parts.query).get("key", [None])[0] != token:
                self.send_error(403)
                return
            if parts.path == "/":
                body = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
                        '<title>媒体冲突检查</title><style>body{font:14px sans-serif;margin:20px}',
                        'table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;',
                        'padding:8px;word-break:break-all}th{background:#eee}a{color:#155ba7}',
                        '</style></head><body><h2>需要人工检查的文件</h2>',
                        f'<p>报告：{html.escape(reports[0].name)}；共 {len(rows)} 项。',
                        '点击“资源管理器选中”即可定位文件，支持中文目录及文件名。</p>',
                        '<table><tr><th>状态</th><th>原因</th><th>错误命名文件</th><th>对应文件</th></tr>']
                for i, row in enumerate(rows):
                    columns = []
                    for side in ("old", "new"):
                        name = html.escape(row.get(side, ""))
                        if current_file(i, side):
                            link = (f'<a target="_blank" href="/select/{i}/{side}?key={token}">'
                                    '资源管理器选中</a>')
                        else:
                            link = '<span>文件不存在或无法定位</span>'
                        columns.append(f'<td>{name}<p>{link}</p></td>')
                    body.append('<tr><td>' + html.escape(row.get("status", "")) +
                                '</td><td>' + html.escape(row.get("reason", "")) +
                                '</td>' + ''.join(columns) + '</tr>')
                body.append('</table></body></html>')
                payload = ''.join(body).encode('utf-8')
            else:
                m = re.fullmatch(r"/select/(\d+)/(old|new)", parts.path)
                if not m:
                    self.send_error(404)
                    return
                path = current_file(int(m.group(1)), m.group(2))
                if path is None:
                    self.send_error(404, "文件已经移动或不存在")
                    return
                try:
                    subprocess.Popen(["explorer.exe", "/select,", str(path)])
                except OSError as exc:
                    self.send_error(500, str(exc))
                    return
                payload = ('<!doctype html><meta charset="utf-8">'
                           '<p>已请求资源管理器选中文件。可以关闭此标签页。</p>').encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = f"http://127.0.0.1:{server.server_port}/?key={token}"
    print(f"[交互问题清单] {reports[0]}")
    print("[定位功能已启动] 请在浏览器点击资源管理器选中。保持此终端开启，结束时按 Ctrl+C。")
    webbrowser.open(address)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def write_report(snap: Path, records: list[dict], mode: str) -> Path:
    """Scan: full CSV only. Apply/resolve_conflicts: full CSV and unresolved issues CSV."""
    report_dir = snap / REPORTS
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_stamp()
    cols = ("kind", "old", "new", "status", "reason", "url")

    def save(path: Path, rows: list[dict], headers=cols) -> None:
        with path.open("x", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    total = report_dir / f"{mode}_{stamp}.csv"
    save(total, records)
    if mode not in ("apply", "resolve_conflicts"):
        return total

    ok_states = {"RENAME", "REPLACE", "TRASH", "DONE_RENAME", "DONE_REPLACE", "DONE_TRASH"}
    issues = [r for r in records if get_status(r) not in ok_states and get_status(r) != "SKIPPED"]
    issues_path = report_dir / f"{mode}_issues_{stamp}.csv"

    # Offline HTML/CSV cannot reliably launch local executables via hyperlinks.
    # Actual clickable selection is provided by --review-issues on localhost.
    def locator(kind: str, name: str) -> tuple[str, str]:
        if kind not in ("image", "video") or not isinstance(name, str):
            return "", ""
        if (not name or name in (".", "..") or Path(name).name != name
                or any(c in name for c in "\\/\x00")):
            return "", ""
        candidate = snap / kind / name
        if candidate.is_symlink() or not candidate.is_file():
            return "", ""
        if candidate.resolve().parent != (snap / kind).resolve():
            return "", ""
        return str(candidate.resolve()), ""

    issue_cols = cols + ("错误文件本地路径", "定位错误文件", "对应文件本地路径", "定位对应文件")
    issue_rows = []
    for rec in issues:
        old_path, _ = locator(rec.get("kind", ""), rec.get("old", ""))
        new_path, _ = locator(rec.get("kind", ""), rec.get("new", ""))
        row = dict(rec)
        row.update({"错误文件本地路径": old_path,
                    "定位错误文件": "运行 --review-issues" if old_path else "文件不存在",
                    "对应文件本地路径": new_path,
                    "定位对应文件": "运行 --review-issues" if new_path else "文件不存在"})
        issue_rows.append(row)
    save(issues_path, issue_rows, issue_cols)

    print(f"[完整报告] {total}（{len(records)} 项）", flush=True)
    print(f"[待人工检查] {issues_path}（{len(issues)} 项）", flush=True)
    print("[可点击定位清单] 在账号目录执行：python ../../fix_convert_media_names.py --review-issues", flush=True)
    return total

def check_recycle(snap: Path) -> tuple[Path, list[dict], list[str]]:
    trash = snap / RECYCLER
    if trash.is_symlink():
        raise RuntimeError("回收文件夹是符号链接，拒绝操作")
    manifest = trash / MANIFEST
    entries: list[dict] = []
    errors: list[str] = []
    if not trash.exists():
        return trash, entries, errors
    if not trash.is_dir():
        raise RuntimeError("回收文件夹路径存在，但不是目录")
    if manifest.is_symlink():
        errors.append("回收清单是符号链接")
    elif manifest.exists():
        seen = set()
        for line_no, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
                if row.get("kind") not in ("image", "video"):
                    raise ValueError("非法 kind")
                for field in ("old", "new"):
                    value = row.get(field)
                    if not isinstance(value, str) or not value or Path(value).name != value or value in (".", ".."):
                        raise ValueError(f"非法 {field} 文件名")
                if not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", "")):
                    raise ValueError("缺少合法 SHA-256")
                key = (row["kind"], row["old"])
                if key in seen:
                    raise ValueError("重复回收清单条目")
                seen.add(key)
                entries.append(row)
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                errors.append(f"回收清单第 {line_no} 行异常：{e}")
    elif any(trash.iterdir()):
        errors.append("回收文件夹非空，但没有回收清单")
    return trash, entries, errors


def write_manifest(trash: Path, entries: list[dict]):
    trash.mkdir(parents=True, exist_ok=True)
    path = trash / MANIFEST
    if path.is_symlink():
        raise RuntimeError("回收清单是符号链接")
    data = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
    atomic_write_bytes(path, data.encode("utf-8"))


class DeleteProgress:
    """控制台进度：交互终端原地刷新，重定向输出定期打印。"""

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.label = ""
        self.total = 0
        self.done = 0
        self.last_print = 0.0
        self.last_width = 0
        self.detail = ""

    def start(self, label: str, total: int):
        self.label, self.total, self.done = label, total, 0
        self.last_print = 0.0
        self.last_width = 0
        self.detail = ""
        self._show(force=True)

    def pulse(self, filename: str):
        self.detail = filename[:65]
        self._show()

    def advance(self):
        self.done += 1
        self.detail = ""
        self._show(force=self.done >= self.total)

    def finish(self):
        if not self.tty and self.done >= self.total:
            return  # 重定向输出时，最后一项已打印过 100%。
        self._show(force=True, finish=True)

    def _show(self, force=False, finish=False):
        now = time.monotonic()
        # 控制台每 0.25 秒刷新，日志/重定向每 5 秒更新，避免海量输出。
        interval = 0.25 if self.tty else 5.0
        if not force and now - self.last_print < interval:
            return
        percent = (self.done * 100 // self.total) if self.total else 100
        msg = f"[{self.label}] {self.done}/{self.total} ({percent}%)"
        if self.detail:
            msg += f" · 正在读取 {self.detail}"
        if self.tty:
            padded = msg.ljust(self.last_width)
            print("\r" + padded, end="\n" if finish else "", flush=True)
            self.last_width = len(msg)
        else:
            print(msg, flush=True)
        self.last_print = now


def validate_trash(snap: Path, trash: Path, entries: list[dict], errors: list[str],
                   check_references: bool = True, progress: DeleteProgress | None = None):
    expected = {(x["kind"], x["old"]) for x in entries}
    actual = set()
    for p in trash.rglob("*"):
        if p.is_symlink():
            errors.append(f"发现符号链接：{p}")
        elif p.is_dir() and p.relative_to(trash).parts not in (("image",), ("video",)):
            errors.append(f"出现额外目录：{p}")
        elif p.is_file() and p != trash / MANIFEST:
            rel = p.relative_to(trash)
            if len(rel.parts) != 2 or rel.parts[0] not in ("image", "video"):
                errors.append(f"发现不属于回收结构的文件：{rel}")
            else:
                actual.add((rel.parts[0], rel.parts[1]))
    if actual != expected:
        errors.append(f"回收文件与清单不一致：未登记 {len(actual-expected)} / 缺失 {len(expected-actual)}")
    if progress:
        progress.start("SHA-256 校验", len(entries))
    for entry in entries:
        try:
            kind, old, new = entry["kind"], entry["old"], entry["new"]
            original = snap / kind / new
            recycled = trash / kind / old
            if (original.is_symlink() or recycled.is_symlink() or
                    not original.is_file() or not recycled.is_file()):
                errors.append(f"正确版或回收版缺失 / 不是普通文件：{kind}/{old} → {new}")
                continue
            kept_hash = entry.get("kept_sha256", entry["sha256"])
            if sha256(original, progress) != kept_hash or sha256(recycled, progress) != entry["sha256"]:
                errors.append(f"回收版或保留版 SHA-256 与操作时记录不一致：{kind}/{old}")
                continue
            policy = entry.get("policy", "exact")
            if policy == "exact":
                if kept_hash != entry["sha256"]:
                    errors.append(f"哈希去重条目内容不同：{kind}/{old}")
            elif policy == "resolution":
                high, low = image_dimensions(original), image_dimensions(recycled)
                if kind != "image" or high is None or low is None or not same_aspect_ratio([high, low]) or high[0]*high[1] <= low[0]*low[1]:
                    errors.append(f"高清替换条目分辨率校验失败：{kind}/{old}")
            elif policy in ("filesize", "filesize_equal"):
                # 对应文件和回收文件仍需逐个 SHA-256 与操作时记录匹配。
                # filesize_equal 只用于冲突专用指令：正确命名文件优先。
                kept_size, recycled_size = original.stat().st_size, recycled.stat().st_size
                if ((policy == "filesize" and kept_size <= recycled_size) or
                        (policy == "filesize_equal" and kept_size != recycled_size)):
                    errors.append(f"保留文件体积校验失败：{kind}/{old}")
            else:
                errors.append(f"未知回收策略 {policy}：{kind}/{old}")
        finally:
            if progress:
                progress.advance()
    if progress:
        progress.finish()
    if check_references and entries:
        mapping = {f"../{e['kind']}/{e['old']}": f"../{e['kind']}/{e['new']}" for e in entries}
        pat = replacement_regex(mapping)
        if progress:
            print("[HTML/index 检查] 正在列出引用文件……", flush=True)
            ref_paths = list(reference_files(snap))
            progress.start("HTML/index 检查", len(ref_paths))
        else:
            ref_paths = reference_files(snap)
        for p in ref_paths:
            try:
                if p.is_symlink() or not p.is_file():
                    errors.append(f"引用文件不是普通文件：{p}")
                    continue
                try:
                    text = p.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError) as e:
                    errors.append(f"引用文件无法读取：{p}: {e}")
                    continue
                if pat.search(text):
                    errors.append(f"HTML/index 仍引用旧文件名：{p}")
            finally:
                if progress:
                    progress.advance()
        if progress:
            progress.finish()


def deletion_hint(args):
    """在账号目录运行的相对命令，方便复制。"""
    cmd = "python ../../fix_convert_media_names.py"
    if args.root != Path.cwd():
        cmd += f' --root "{args.root.resolve()}"'
    return cmd + " --delete-trash"


def delete_trash(snap: Path) -> int:
    print("[开始] 正在读取回收清单……", flush=True)
    trash, entries, errors = check_recycle(snap)
    if not trash.exists():
        print("回收文件夹不存在，无需删除。")
        return 0
    print(f"[回收清单] {len(entries)} 个文件；正在检查目录结构……", flush=True)
    progress = DeleteProgress()
    validate_trash(snap, trash, entries, errors, progress=progress)
    if errors:
        print("[删除已拒绝] 回收目录检查失败：")
        for e in errors[:20]:
            print(" -", e)
        return 2

    # 只删除已校验清单中的文件及其固定目录，逐项显示删除进度。
    # 不使用无进度输出的 shutil.rmtree，也不追踪符号链接。
    targets = [trash / e["kind"] / e["old"] for e in entries]
    manifest = trash / MANIFEST
    if manifest.is_file() and not manifest.is_symlink():
        targets.append(manifest)
    print(f"[校验通过] 准备删除 {len(entries)} 个回收文件。", flush=True)
    progress.start("删除文件", len(targets))
    try:
        for target in targets:
            if target.is_symlink() or not target.is_file():
                raise RuntimeError(f"删除前文件发生变化：{target}")
            target.unlink()
            progress.advance()
        progress.finish()
        for dirname in ("image", "video"):
            folder = trash / dirname
            if folder.is_symlink():
                raise RuntimeError(f"删除前目录发生变化：{folder}")
            if folder.exists():
                folder.rmdir()  # 目录若被额外加入文件，拒绝递归删除。
        trash.rmdir()
    except (OSError, RuntimeError) as e:
        print(f"\n[删除中断] {e}")
        print("部分文件可能已经删除；请检查回收目录，不要直接重复执行删除。")
        return 2
    print("回收文件夹已删除。")
    return 0


def delete_reference_backups(snap: Path) -> int:
    """删除本工具创建的 HTML/index 原版备份，不影响现有 HTML/index。"""
    root = snap / BACKUPS
    if root.is_symlink():
        print("[删除已拒绝] 备份目录是符号链接。")
        return 2
    if not root.exists():
        print("HTML/index 备份目录不存在，无需删除。")
        return 0
    if not root.is_dir():
        print("[删除已拒绝] 备份路径不是目录。")
        return 2
    batches = list(root.iterdir())
    for batch in batches:
        if batch.is_symlink() or not batch.is_dir() or not re.fullmatch(r"\d{8}_\d{6}_\d{6}", batch.name):
            print(f"[删除已拒绝] 存在未知备份批次：{batch}")
            return 2
        for item in batch.rglob("*"):
            if item.is_symlink():
                print(f"[删除已拒绝] 备份内存在符号链接：{item}")
                return 2
            rel = item.relative_to(batch)
            if item.is_file():
                if not (rel == Path("index.json") or
                        (len(rel.parts) == 2 and rel.parts[0] == "html" and item.suffix.lower() == ".html")):
                    print(f"[删除已拒绝] 存在未知备份文件：{item}")
                    return 2
            elif not item.is_dir():
                print(f"[删除已拒绝] 存在未知文件类型：{item}")
                return 2
    count = sum(1 for batch in batches for item in batch.rglob("*") if item.is_file())
    shutil.rmtree(root)
    print(f"HTML/index 原版备份已删除（{count} 个文件）：{root}")
    return 0



def runall(root: Path) -> int:
    """依次扫描、修复、校验回收站并清理备份；未处理的冲突仅保留在报告中。"""
    account = root.resolve()
    python = sys.executable
    this_script = str(Path(__file__).resolve())

    def stage(label: str, *extra: str) -> bool:
        print(f"\n{'=' * 15} [runall] {label} {'=' * 15}", flush=True)
        command = [python, this_script, "--root", str(account), *extra]
        result = subprocess.run(command, check=False)
        if result.returncode:
            print(f"[runall 已停止] {label}执行失败（退出码 {result.returncode}），不会继续自动清理。", flush=True)
            return False
        return True

    if not stage("1/4 扫描", "--strict-scan"):
        return 2
    if not stage("2/4 修复", "--apply"):
        return 2

    # CONFLICT / UNVERIFIED / AMBIGUOUS 没有被移动，不影响已经成功处理项目的清理。
    # 仍由 --delete-trash 执行全部清单、哈希和引用校验；校验失败时停止。
    if not stage("3/4 删除回收站", "--delete-trash"):
        return 2
    if not stage("4/4 删除 HTML/index 备份", "--delete-backups"):
        return 2
    print("\n[runall 完成] 四个步骤均已成功执行；未处理的冲突文件仍留在原处并记录于报告。", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="修复旧 convert 的图片/视频命名及 HTML、index.json 引用")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="账号目录或 wayback_snapshots 目录；默认终端当前目录")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="真正执行移动/改名/引用更新；默认只扫描")
    group.add_argument("--delete-trash", action="store_true", help="SHA-256 和引用检查通过后，直接删除回收文件")
    group.add_argument("--delete-backups", action="store_true", help="删除本工具生成的 HTML/index 原版备份")
    group.add_argument("--runall", action="store_true", help="顺序扫描、修复、清理回收站及备份")
    group.add_argument("--review-issues", action="store_true", help="在浏览器打开可直接定位到资源管理器的问题清单")
    group.add_argument("--resolve-conflicts", action="store_true", help="仅处理 CONFLICT：按体积择大，相同体积优先正确命名版本")
    group.add_argument("--select-file", metavar="TOKEN", help=argparse.SUPPRESS)
    ap.add_argument("--strict-scan", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.runall:
        return runall(args.root)
    snap = args.root.resolve()
    if (snap / "wayback_snapshots").is_dir():
        snap = snap / "wayback_snapshots"
    if args.select_file is not None:
        return locate_in_explorer(snap, args.select_file)
    if args.review_issues:
        return review_issues(snap)
    if args.delete_trash:
        return delete_trash(snap)
    if args.delete_backups:
        return delete_reference_backups(snap)
    print(f"[存档目录] {snap}")
    snap, found, suspicious, n_files, n_json, n_bad_json = discover(args.root.resolve())
    print(f"[扫描] 媒体 {n_files} 个，JSON 已读取 {n_json} 个，失败 {n_bad_json} 个")
    records = list(found.values()) + suspicious
    if args.resolve_conflicts:
        # 首先按既有规则分类，确定哪些确实属于 CONFLICT。
        classify(snap, found)
        originally_conflicted = {key for key, rec in found.items() if get_status(rec) == "CONFLICT"}
        # 再单独对此类候选进行更宽松的体积决策，普通 --apply 逻辑完全不变。
        conflict_found = {key: rec for key, rec in found.items() if key in originally_conflicted}
        for rec in conflict_found.values():
            set_status(rec, "PENDING")
        classify(snap, conflict_found, resolve_conflicts=True)
        conflicted_ids = {id(rec) for rec in conflict_found.values()}
        for rec in records:
            if id(rec) not in conflicted_ids and get_status(rec) in ("RENAME", "TRASH", "REPLACE"):
                set_status(rec, "SKIPPED")
                rec["reason"] = "冲突专用指令：普通可修复项留给 --apply 处理"
    else:
        classify(snap, found)
    stats = Counter(r["status"] for r in records)
    print("[检查结果]", ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "无旧版错误文件")
    for rec in sorted(records, key=lambda r: (r["status"], r["kind"], r["old"]))[:24]:
        print(f"  [{rec['status']:<10}] {rec['kind']}/{rec['old']}")
        if rec["new"]:
            print(f"                 → {rec['new']}")
    if len(records) > 24:
        print(f"  ...其余 {len(records)-24} 条见 CSV 报告")
    if n_bad_json:
        print("[提醒] 有 JSON 解析失败；依赖这些 JSON 的媒体可能未被识别。")
    if not (args.apply or args.resolve_conflicts):
        report = write_report(snap, records, "scan")
        print(f"[扫描报告] {report}")
        print("[只读预览完成] 未移动或更名媒体，未修改 HTML/index/推文 JSON。")
        print("确认报告后，在账号目录执行：python ../../fix_convert_media_names.py --apply")
        if args.strict_scan and n_bad_json:
            print("[runall 已停止] 存在 JSON 解析失败，暂不自动修改或清理。")
            return 2
        return 0

    trash, previous_entries, trash_errors = check_recycle(snap)
    if trash_errors:
        print("[停止] 原回收文件夹异常：", "; ".join(trash_errors))
        return 2
    # 有未删除的回收文件时，先验证是否完整；避免重复运行误覆盖和引用错位。
    if trash.exists() and previous_entries:
        validate_trash(snap, trash, previous_entries, trash_errors, check_references=False)
        if trash_errors:
            print("[停止] 旧回收文件校验未通过：", "; ".join(trash_errors[:5]))
            return 2
    existing_names = {(r["kind"], r["old"]) for r in previous_entries}
    actions = [r for r in records if get_status(r) in ("RENAME", "TRASH", "REPLACE")]
    for r in actions:
        if get_status(r) in ("TRASH", "REPLACE") and ((r["kind"], r["old"]) in existing_names or
                                        (trash / r["kind"] / r["old"]).exists()):
            print(f"[停止] 回收目录已存在此文件：{r['kind']}/{r['old']}")
            return 2
    for r in actions:
        if get_status(r) == "REPLACE":
            recycled_name = r["displaced_name"]
            if ((r["kind"], recycled_name) in existing_names or
                    (trash / r["kind"] / recycled_name).exists() or
                    (trash / r["kind"] / recycled_name).is_symlink()):
                print(f"[停止] 大文件替换的回收目标已存在：{r['kind']}/{recycled_name}")
                return 2
    mapping = {f"../{e['kind']}/{e['old']}": f"../{e['kind']}/{e['new']}" for e in previous_entries}
    for r in actions:
        k = f"../{r['kind']}/{r['old']}"
        value = f"../{r['kind']}/{r['new']}"
        if k in mapping and mapping[k] != value:
            print("[停止] 引用映射冲突：", k)
            return 2
        mapping[k] = value
    try:
        updates = prepare_reference_updates(snap, mapping)
    except (OSError, RuntimeError, UnicodeError, ValueError) as e:
        print(f"[停止] 引用预检查失败，未移动任何媒体：{e}")
        return 2
    backup_root = snap / BACKUPS / utc_stamp()
    try:
        save_reference_backups(snap, updates, backup_root)
    except OSError as e:
        print(f"[停止] 备份 HTML/index 失败，未移动任何媒体：{e}")
        return 2
    if not actions and not updates:
        print("没有能够安全自动修复的文件或残留旧引用。")
        report = write_report(snap, records, "resolve_conflicts" if args.resolve_conflicts else "apply")
        if previous_entries:
            print("[下一步] 检查回收文件夹后执行：", deletion_hint(args))
        return 0

    successful_moves: list[tuple[Path, Path]] = []
    completed_actions = []
    current_entries = list(previous_entries)
    modified_refs = []
    error = None
    try:
        # 大文件替换先移走原目标，再把更大版本改成规范名；其它重复项随后回收。
        priorities = {"REPLACE": 0, "RENAME": 1, "TRASH": 2}
        for r in sorted(actions, key=lambda r: (priorities[get_status(r)], r["kind"], r["old"])):
            kind, old, new = r["kind"], r["old"], r["new"]
            action = get_status(r)
            src, dst = snap / kind / old, snap / kind / new
            if not src.is_file() or src.is_symlink() or sha256(src) != r["source_sha256"]:
                raise RuntimeError(f"源文件已变更，停止操作：{src}")
            if action == "REPLACE":
                if (not dst.is_file() or dst.is_symlink() or
                        sha256(dst) != r["target_sha256_before"]):
                    raise RuntimeError(f"待替换的正确版已变更：{dst}")
                if src.stat().st_size <= dst.stat().st_size:
                    raise RuntimeError(f"待替换的源文件不再比正确命名版本更大：{src}")
                recycle_path = trash / kind / r["displaced_name"]
                recycle_path.parent.mkdir(parents=True, exist_ok=True)
                if recycle_path.exists() or recycle_path.is_symlink():
                    raise RuntimeError(f"回收目标已存在：{recycle_path}")
                dst.rename(recycle_path)
                successful_moves.append((dst, recycle_path))
                src.rename(dst)
                successful_moves.append((src, dst))
                current_entries.append(manifest_entry(kind, r["displaced_name"], new,
                                                      recycle_path, dst, "filesize"))
                write_manifest(trash, current_entries)
            elif action == "RENAME":
                if dst.exists() or dst.is_symlink():
                    raise RuntimeError(f"目标文件已存在，拒绝覆盖：{dst}")
                src.rename(dst)
                successful_moves.append((src, dst))
            else:
                if not dst.is_file() or dst.is_symlink():
                    raise RuntimeError(f"正确命名版本缺失：{dst}")
                policy = r.get("policy", "exact")
                if policy == "exact":
                    if sha256(src) != sha256(dst):
                        raise RuntimeError(f"执行前 SHA-256 校验未通过：{src} → {dst}")
                elif policy == "filesize":
                    if (not args.resolve_conflicts and kind != "image") or src.stat().st_size >= dst.stat().st_size:
                        raise RuntimeError(f"执行前文件体积比较未通过：{src}")
                elif policy == "filesize_equal":
                    if not args.resolve_conflicts or src.stat().st_size != dst.stat().st_size:
                        raise RuntimeError(f"执行前等体积比较未通过：{src}")
                else:
                    raise RuntimeError(f"未知回收策略：{policy}")
                recycle_path = trash / kind / old
                recycle_path.parent.mkdir(parents=True, exist_ok=True)
                if recycle_path.exists() or recycle_path.is_symlink():
                    raise RuntimeError(f"回收目标已存在：{recycle_path}")
                src.rename(recycle_path)
                successful_moves.append((src, recycle_path))
                current_entries.append(manifest_entry(kind, old, new, recycle_path, dst, policy))
                write_manifest(trash, current_entries)
            completed_actions.append(r)
            set_status(r, "DONE_" + action)

        for p, raw, encoded in updates:
            atomic_write_bytes(p, encoded)
            modified_refs.append((p, raw))
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        error = exc

    if error is not None:
        print(f"[操作失败] {error}；正在尝试恢复文件和引用……")
        rollback_errors = []
        for p, original in reversed(modified_refs):
            try:
                atomic_write_bytes(p, original)
            except OSError as exc:
                rollback_errors.append(f"引用还原失败 {p}: {exc}")
        for src, moved_path in reversed(successful_moves):
            try:
                if src.exists() or src.is_symlink() or not moved_path.is_file():
                    raise RuntimeError("原路径被占用或已移动文件不见了")
                moved_path.rename(src)
            except (OSError, RuntimeError) as exc:
                rollback_errors.append(f"媒体还原失败 {moved_path}: {exc}")
        for r in completed_actions:
            set_status(r, "ROLLED_BACK")
        if not rollback_errors:
            try:
                if trash.exists() and (trash / MANIFEST).exists():
                    write_manifest(trash, previous_entries)
            except OSError as exc:
                rollback_errors.append(f"回收清单还原失败：{exc}")
        report = write_report(snap, records, "resolve_conflicts_failed" if args.resolve_conflicts else "failed")
        if rollback_errors:
            for msg in rollback_errors:
                print("[需要人工核对]", msg)
        else:
            print("已将本次变更回退；原有回收文件不会受影响。")
        print("本次有失败项，暂时不要删除回收文件夹。")
        return 2

    report = write_report(snap, records, "resolve_conflicts" if args.resolve_conflicts else "apply")
    print(f"[修复] 更名 {sum(get_status(r)=='DONE_RENAME' for r in records)} 个，"
          f"大文件替换 {sum(get_status(r)=='DONE_REPLACE' for r in records)} 个，"
          f"旧文件回收 {sum(get_status(r)=='DONE_TRASH' for r in records)} 个；"
          f"引用更新 {len(modified_refs)} 个文件")
    if updates:
        print("[HTML/index 原版备份]", backup_root)
        print("[备份检查无误后可删除] python ../../fix_convert_media_names.py --delete-backups")
    unresolved = sum(get_status(r) in ("AMBIGUOUS", "CONFLICT", "UNVERIFIED") for r in records)
    if unresolved:
        print(f"[提醒] {unresolved} 个不确定 / 哈希冲突文件保留原状，详见报告。")
    if current_entries:
        print(f"\n[下一步] 先检查回收文件夹：{trash}")
        print("确认内容没问题后，在账号目录复制执行下面的命令：")
        print("  " + deletion_hint(args))
        print("脚本会再次校验 SHA-256、正确文件及 HTML/index 引用；校验通过后直接删除，无需二次输入。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, KeyboardInterrupt) as e:
        print(f"[中止] {e}")
        sys.exit(2)
