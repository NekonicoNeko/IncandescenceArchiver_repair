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
  python ../../fix_convert_media_names.py --delete-trash     # 用户确认后删除回收文件夹
  python ../../fix_convert_media_names.py --root <账号目录> --apply

Python >= 3.10，标准库，无第三方依赖。
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
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


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024 * 4), b""):
            h.update(chunk)
    return h.hexdigest()


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
                                            url="", thumbnail=thumbnail)
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


def classify(snap: Path, found: dict):
    """预计算 SHA-256；冲突时拒绝动任何一份差异文件。"""
    groups = defaultdict(list)
    for rec in found.values():
        if get_status(rec) == "PENDING":
            groups[(rec["kind"], rec["new"])].append(rec)
    for (kind, target_name), members in groups.items():
        folder = snap / kind
        target = folder / target_name
        if target.is_symlink():
            for r in members:
                set_status(r, "CONFLICT")
                r["reason"] = "目标文件是符号链接"
            continue
        paths = [folder / r["old"] for r in members]
        if target.exists():
            if not target.is_file():
                for r in members:
                    set_status(r, "CONFLICT")
                    r["reason"] = "目标路径已经存在且不是文件"
                continue
            paths.append(target)
        try:
            # size check first; same size still requires cryptographic hash
            sizes = {p.stat().st_size for p in paths}
            if len(sizes) != 1:
                raise ValueError("文件大小不同")
            digest = {sha256(p) for p in paths}
            if len(digest) != 1:
                raise ValueError("SHA-256 不一致")
        except (OSError, ValueError) as e:
            for r in members:
                set_status(r, "CONFLICT")
                r["reason"] = str(e)
            continue
        if target.exists():
            for r in members:
                set_status(r, "TRASH")
                r["reason"] = "正确文件已存在且 SHA-256 一致"
        else:
            first = sorted(members, key=lambda r: r["old"])[0]
            set_status(first, "RENAME")
            first["reason"] = "正确文件尚不存在"
            for r in members:
                if r is first:
                    continue
                set_status(r, "TRASH")
                r["reason"] = "多份旧文件目标相同且 SHA-256 一致；保留一份改名"


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


def write_report(snap: Path, records: list[dict], mode: str) -> Path:
    report_dir = snap / REPORTS
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{mode}_{utc_stamp()}.csv"
    cols = ("kind", "old", "new", "status", "reason", "url")
    with path.open("x", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    return path


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


def validate_trash(snap: Path, trash: Path, entries: list[dict], errors: list[str],
                   check_references: bool = True):
    expected = {(x["kind"], x["old"]) for x in entries}
    actual = set()
    for p in trash.rglob("*"):
        if p.is_symlink():
            errors.append(f"发现符号链接：{p}")
        elif p.is_dir() and p.relative_to(trash).parts not in (("image",), ("video",)):
            errors.append(f"出现额外目录：{p}")
        elif p.is_file() and p.name != MANIFEST:
            rel = p.relative_to(trash)
            if len(rel.parts) != 2 or rel.parts[0] not in ("image", "video"):
                errors.append(f"发现不属于回收结构的文件：{rel}")
            else:
                actual.add((rel.parts[0], rel.parts[1]))
    if actual != expected:
        errors.append(f"回收文件与清单不一致：未登记 {len(actual-expected)} / 缺失 {len(expected-actual)}")
    for entry in entries:
        kind, old, new = entry["kind"], entry["old"], entry["new"]
        original = snap / kind / new
        recycled = trash / kind / old
        if original.is_symlink() or recycled.is_symlink() or not original.is_file() or not recycled.is_file():
            errors.append(f"正确版或回收版缺失 / 不是普通文件：{kind}/{old} → {new}")
            continue
        if sha256(original) != entry["sha256"] or sha256(recycled) != entry["sha256"]:
            errors.append(f"回收版与现存正确版 SHA-256 不一致：{kind}/{old}")
    if check_references and entries:
        mapping = {f"../{e['kind']}/{e['old']}": f"../{e['kind']}/{e['new']}" for e in entries}
        pat = replacement_regex(mapping)
        for p in reference_files(snap):
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


def deletion_hint(args):
    """在账号目录运行的相对命令，方便复制。"""
    cmd = "python ../../fix_convert_media_names.py"
    if args.root != Path.cwd():
        cmd += f' --root "{args.root.resolve()}"'
    return cmd + " --delete-trash"


def delete_trash(snap: Path) -> int:
    trash, entries, errors = check_recycle(snap)
    if not trash.exists():
        print("回收文件夹不存在，无需删除。")
        return 0
    validate_trash(snap, trash, entries, errors)
    if errors:
        print("[删除已拒绝] 回收目录检查失败：")
        for e in errors[:20]:
            print(" -", e)
        return 2
    print(f"准备删除 {trash}（共 {len(entries)} 个回收文件）")
    confirm = input('确认删除请输入 DELETE：').strip()
    if confirm != "DELETE":
        print("未删除。")
        return 0
    shutil.rmtree(trash)
    print("回收文件夹已删除。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="修复旧 convert 的图片/视频命名及 HTML、index.json 引用")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="账号目录或 wayback_snapshots 目录；默认终端当前目录")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="真正执行移动/改名/引用更新；默认只扫描")
    group.add_argument("--delete-trash", action="store_true", help="SHA-256 和引用检查通过后，确认删除回收文件")
    args = ap.parse_args()
    snap = args.root.resolve()
    if (snap / "wayback_snapshots").is_dir():
        snap = snap / "wayback_snapshots"
    if args.delete_trash:
        return delete_trash(snap)
    print(f"[存档目录] {snap}")
    snap, found, suspicious, n_files, n_json, n_bad_json = discover(args.root.resolve())
    print(f"[扫描] 媒体 {n_files} 个，JSON 已读取 {n_json} 个，失败 {n_bad_json} 个")
    records = list(found.values()) + suspicious
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
    if not args.apply:
        report = write_report(snap, records, "scan")
        print(f"[扫描报告] {report}")
        print("[只读预览完成] 未移动或更名媒体，未修改 HTML/index/推文 JSON。")
        print("确认报告后，在账号目录执行：python ../../fix_convert_media_names.py --apply")
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
    actions = [r for r in records if get_status(r) in ("RENAME", "TRASH")]
    for r in actions:
        if get_status(r) == "TRASH" and ((r["kind"], r["old"]) in existing_names or
                                        (trash / r["kind"] / r["old"]).exists()):
            print(f"[停止] 回收目录已存在此文件：{r['kind']}/{r['old']}")
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
        report = write_report(snap, records, "apply")
        print("[报告]", report)
        if previous_entries:
            print("[下一步] 检查回收文件夹后执行：", deletion_hint(args))
        return 0

    successful_moves = []
    current_entries = list(previous_entries)
    modified_refs = []
    error = None
    try:
        # 先重命名一份，再归档同名且哈希相同的重复文件。
        for r in sorted(actions, key=lambda r: (0 if get_status(r) == "RENAME" else 1, r["kind"], r["old"])):
            kind, old, new = r["kind"], r["old"], r["new"]
            src, dst = snap / kind / old, snap / kind / new
            if not src.is_file() or src.is_symlink():
                raise RuntimeError(f"源文件已变更或是符号链接：{src}")
            if get_status(r) == "RENAME":
                if dst.exists() or dst.is_symlink():
                    raise RuntimeError(f"目标文件已存在，拒绝覆盖：{dst}")
                src.rename(dst)
                successful_moves.append((r, src, dst))
            else:
                if not dst.is_file() or dst.is_symlink() or sha256(src) != sha256(dst):
                    raise RuntimeError(f"执行前 SHA-256 校验未通过：{src} → {dst}")
                recycle_path = trash / kind / old
                recycle_path.parent.mkdir(parents=True, exist_ok=True)
                if recycle_path.exists() or recycle_path.is_symlink():
                    raise RuntimeError(f"回收目标已存在：{recycle_path}")
                src.rename(recycle_path)
                successful_moves.append((r, src, recycle_path))
                current_entries.append(dict(kind=kind, old=old, new=new, sha256=sha256(recycle_path)))
                write_manifest(trash, current_entries)
            set_status(r, "DONE_" + get_status(r))

        for p, raw, encoded in updates:
            atomic_write_bytes(p, encoded)
            modified_refs.append((p, raw))
    except (OSError, RuntimeError) as exc:
        error = exc

    if error is not None:
        print(f"[操作失败] {error}；正在尝试恢复文件和引用……")
        rollback_errors = []
        for p, original in reversed(modified_refs):
            try:
                atomic_write_bytes(p, original)
            except OSError as exc:
                rollback_errors.append(f"引用还原失败 {p}: {exc}")
        for r, src, moved_path in reversed(successful_moves):
            try:
                if src.exists() or src.is_symlink() or not moved_path.is_file():
                    raise RuntimeError("原路径被占用或已移动文件不见了")
                moved_path.rename(src)
                set_status(r, "ROLLED_BACK")
                current_entries = [e for e in current_entries if (e["kind"], e["old"]) != (r["kind"], r["old"])]
            except (OSError, RuntimeError) as exc:
                rollback_errors.append(f"媒体还原失败 {moved_path}: {exc}")
        try:
            if trash.exists() and (trash / MANIFEST).exists():
                write_manifest(trash, current_entries)
        except OSError as exc:
            rollback_errors.append(f"回收清单还原失败：{exc}")
        report = write_report(snap, records, "failed")
        print(f"[报告] {report}")
        if rollback_errors:
            for msg in rollback_errors:
                print("[需要人工核对]", msg)
        else:
            print("已将本次变更回退；原有回收文件不会受影响。")
        print("本次有失败项，暂时不要删除回收文件夹。")
        return 2

    report = write_report(snap, records, "apply")
    print(f"[修复] 更名 {sum(get_status(r)=='DONE_RENAME' for r in records)} 个，"
          f"移至回收 {sum(get_status(r)=='DONE_TRASH' for r in records)} 个；"
          f"引用更新 {len(modified_refs)} 个文件")
    print("[报告]", report)
    if updates:
        print("[HTML/index 原版备份]", backup_root)
    unresolved = sum(get_status(r) in ("AMBIGUOUS", "CONFLICT", "UNVERIFIED") for r in records)
    if unresolved:
        print(f"[提醒] {unresolved} 个不确定 / 哈希冲突文件保留原状，详见报告。")
    if current_entries:
        print(f"\n[下一步] 先检查回收文件夹：{trash}")
        print("确认内容没问题后，在账号目录复制执行下面的命令：")
        print("  " + deletion_hint(args))
        print("脚本会再次校验 SHA-256、正确文件及 HTML/index 引用，并要求输入 DELETE。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, KeyboardInterrupt) as e:
        print(f"[中止] {e}")
        sys.exit(2)
