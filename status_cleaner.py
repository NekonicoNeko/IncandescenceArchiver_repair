#!/usr/bin/env python3
"""Reconcile IncandescenceArchiver status records against files actually on disk.

Only changes the `status` field of existing records in _log/archive_index.json,
then regenerates the corresponding status .txt views. It never downloads,
renames, deletes, or edits HTML/JSON/media/profile files.

Usage (from accounts/<username>):
  python ..\\..\\status_cleaner.py scan
  python ..\\..\\status_cleaner.py apply
"""
from __future__ import annotations
import argparse
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

KINDS = {
    "html": ("html_done.txt", "html_failed.txt", "html_failed_all.txt"),
    "media": ("media_done.txt", "media_failed.txt", "media_failed_all.txt"),
    "images": ("image_done.txt", "image_failed.txt", "image_failed_all.txt"),
    "videos": ("video_done.txt", "video_failed.txt", "video_failed_all.txt"),
    "avatars": ("avatar_done.txt", "avatar_failed.txt", "avatar_failed_all.txt"),
}
VALID_STATUSES = {"done", "failed", "failed_all"}
IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
IMG_NEW_RE = re.compile(r"_media_([A-Za-z0-9_\-]+?)_(?:jpg|png|gif|webp|jpeg)\.(?:jpg|png|gif|webp|jpeg)$", re.I)
IMG_OLD_RE = re.compile(r"_media_([A-Za-z0-9_\-]+?)\.(?:jpg|png|gif|webp|jpeg)$", re.I)
IMG_URL_RE = re.compile(r"pbs\.twimg\.com/media/([A-Za-z0-9_\-]+?)(?:\.(?:jpg|png|gif|webp|jpeg))?$", re.I)
VIDEO_RE = re.compile(r"(?:amplify_video|ext_tw_video)[/_](\d+)|tweet_video[/_]([A-Za-z0-9_-]+)", re.I)
PID_RE = re.compile(r"/profile_images/(\d+)/")
WAYBACK_RE = re.compile(r"^https?://web\.archive\.org/web/(\d{14})(?:if_|im_|id_)?/(.+)$", re.I)


def locate_root() -> Path:
    cwd = Path.cwd()
    root = cwd / "wayback_snapshots"
    if (root / "_log" / "archive_index.json").is_file():
        return root
    accounts = cwd / "accounts"
    if accounts.is_dir():
        candidates = [p / "wayback_snapshots" for p in accounts.iterdir()
                      if p.is_dir() and (p / "wayback_snapshots" / "_log" / "archive_index.json").is_file()]
        if len(candidates) == 1:
            return candidates[0]
    raise RuntimeError("找不到 wayback_snapshots\\_log\\archive_index.json。请在 accounts\\用户名 目录运行。")


def good_file(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def image_key(value: str) -> str:
    s = str(value or "").split("?", 1)[0]
    for rx in (IMG_NEW_RE, IMG_OLD_RE, IMG_URL_RE):
        m = rx.search(s)
        if m:
            return m.group(1)
    return ""


def video_key(value: str) -> str:
    m = VIDEO_RE.search(str(value or ""))
    return (m.group(1) or m.group(2)) if m else ""


def avatar_pid(value: str) -> str:
    m = PID_RE.search(str(value or ""))
    return m.group(1) if m else ""


def safe_filename(timestamp: str, url: str, ext: str) -> str:
    # Mirrors archive.py's safe_filename implementation.
    url_no_query = url.split("?", 1)[0]
    clean = re.sub(r"[^\w\-_]", "_", url_no_query.replace("https://", "").replace("http://", ""))
    return f"{timestamp}_{clean[:100]}{ext}"


def html_local_path(key: str, html_dir: Path) -> Path | None:
    if key.startswith("local://"):
        return html_dir / key[len("local://"):]
    m = WAYBACK_RE.match(key)
    if not m:
        return None
    timestamp, original_url = m.groups()
    # The URL stored in archive_index is a Wayback URL. Its original URL is the
    # part after the timestamp/modifier, and the local filename is deterministic.
    return html_dir / safe_filename(timestamp, original_url, ".html")


def index_local_files(root: Path):
    image_files: dict[str, list[Path]] = defaultdict(list)
    video_files: dict[str, list[Path]] = defaultdict(list)
    avatar_files: dict[str, list[Path]] = defaultdict(list)
    for p in (root / "image").iterdir() if (root / "image").is_dir() else []:
        if not good_file(p):
            continue
        key = image_key(p.name)
        if key:
            image_files[key].append(p)
    for p in (root / "video").iterdir() if (root / "video").is_dir() else []:
        if not good_file(p):
            continue
        key = video_key(p.name)
        if key:
            video_files[key].append(p)
    for p in (root / "avatar").iterdir() if (root / "avatar").is_dir() else []:
        if not good_file(p) or p.suffix.lower() not in IMG_EXTS:
            continue
        m = re.match(r"^avatar_(\d+)\.", p.name, re.I)
        if m:
            avatar_files[m.group(1)].append(p)
    return image_files, video_files, avatar_files


def load_json_refs(json_path: Path):
    """Return resource keys and mapping problems using archive.py's extraction rules."""
    data = json.loads(json_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("JSON 根节点不是对象")
    includes = data.get("includes") or {}
    if not isinstance(includes, dict):
        includes = {}
    images, videos, avatars = set(), set(), set()
    mapping_problems = []
    for media in includes.get("media") or []:
        if not isinstance(media, dict):
            continue
        mtype = media.get("type", "")
        if mtype == "photo":
            url = media.get("url") or ""
            key = image_key(url)
            if key:
                images.add(key)
            elif url:
                mapping_problems.append(f"无法从图片 URL 提取 basename：{url}")
        elif mtype in ("video", "animated_gif"):
            # Mirror archive.py::_iter_media_in_json exactly: scan in source order,
            # accept video/mp4 only, and let a later variant win equal bit rates.
            best_url = None
            best_bit_rate = -1
            for variant in media.get("variants") or []:
                if not isinstance(variant, dict) or variant.get("content_type") != "video/mp4":
                    continue
                url = variant.get("url")
                if not url:
                    continue
                bit_rate = variant.get("bit_rate", 0) or 0
                if bit_rate >= best_bit_rate:
                    best_bit_rate = bit_rate
                    best_url = url
            if best_url:
                key = video_key(best_url)
                if key:
                    videos.add(key)
                else:
                    mapping_problems.append(f"无法从视频 URL 提取 media key：{best_url}")
    for user in includes.get("users") or []:
        if not isinstance(user, dict):
            continue
        url = user.get("profile_image_url") or ""
        pid = avatar_pid(url)
        if pid:
            avatars.add(pid)
        elif url:
            mapping_problems.append(f"无法从头像 URL 提取 PID：{url}")
    return images, videos, avatars, mapping_problems


def compute_file_maps(root: Path):
    image_files, video_files, avatar_files = index_local_files(root)
    html_dir = root / "html"
    json_dir = root / "json"
    return image_files, video_files, avatar_files, html_dir, json_dir


def desired_status(kind: str, key: str, rec: dict, root: Path, maps, json_cache, errors):
    image_files, video_files, avatar_files, html_dir, json_dir = maps
    current = rec.get("status")
    exists: bool | None = None
    reason = ""
    if kind == "html":
        path = html_local_path(key, html_dir)
        if path is None:
            return None, "无法从 HTML 状态键解析本地文件名"
        exists = good_file(path)
        reason = (f"HTML 文件存在且非空：{path}" if exists
                  else f"本地 HTML 文件缺失或为空：{path}")
    elif kind == "images":
        k = image_key(key)
        if not k:
            return None, "无法从图片 URL 提取 basename"
        matches = image_files.get(k, [])
        exists = bool(matches)
        reason = (f"图片 key={k}；本地匹配文件：{', '.join(str(p) for p in matches)}"
                  if exists else f"图片 key={k} 在本地 image 目录中没有对应的非空文件")
    elif kind == "videos":
        k = video_key(key)
        if not k:
            return None, "无法从视频 URL 提取 media key"
        matches = video_files.get(k, [])
        exists = bool(matches)
        reason = (f"视频 key={k}；本地匹配文件：{', '.join(str(p) for p in matches)}"
                  if exists else f"视频 key={k} 在本地 video 目录中没有对应的非空文件")
    elif kind == "avatars":
        k = avatar_pid(key)
        if not k:
            return None, "无法从头像 URL 提取 PID"
        matches = avatar_files.get(k, [])
        exists = bool(matches)
        reason = (f"头像 PID={k}；本地匹配文件：{', '.join(str(p) for p in matches)}"
                  if exists else f"头像 PID={k} 在本地 avatar 目录中没有对应的非空文件")
    elif kind == "media":
        # media keys are JSON filenames. Check each referenced file, not just the
        # old depends_on table, so missing dependencies in that table are caught.
        jp = json_dir / key
        if not good_file(jp):
            exists = False
            reason = f"JSON 文件缺失或为空：{jp}"
        else:
            if key not in json_cache:
                try:
                    refs = load_json_refs(jp)
                    mapping_problems = refs[3]
                    json_cache[key] = (refs, ("资源 URL 无法映射到本地命名规则：" + "；".join(mapping_problems)) if mapping_problems else None)
                except Exception as e:
                    json_cache[key] = (None, f"{type(e).__name__}: {e}")
            refs, err = json_cache[key]
            if err:
                errors.append((key, err))
                return None, f"无法可靠检查 JSON 引用，保留原状态：{err}"
            imgs, vids, avs, _mapping_problems = refs
            missing = []
            missing.extend(f"image:{x}" for x in imgs if not image_files.get(x))
            missing.extend(f"video:{x}" for x in vids if not video_files.get(x))
            missing.extend(f"avatar:{x}" for x in avs if not avatar_files.get(x))
            exists = not missing
            reason = "所有引用文件均存在" if exists else "缺失引用文件：" + ", ".join(missing)
    else:
        return None, "未知类型"

    if exists:
        return "done", reason
    # Preserve retryable `failed` exactly as requested. Any other state for a
    # missing local file becomes failed_all.
    if current == "failed":
        return "failed", reason
    return "failed_all", reason


def read_txt_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def backup_files(log_dir: Path, index_path: Path):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = log_dir / f"status_cleaner_backup_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    paths = [index_path] + [log_dir / fn for triplet in KINDS.values() for fn in triplet]
    for p in paths:
        if p.is_file():
            shutil.copy2(p, backup_dir / p.name)
    return backup_dir


def build_txt_contents(index: dict):
    contents = {fn: set() for triplet in KINDS.values() for fn in triplet}
    for kind, triplet in KINDS.items():
        done_fn, failed_fn, failed_all_fn = triplet
        for key, rec in (index.get(kind) or {}).items():
            if not isinstance(rec, dict):
                continue
            status = rec.get("status")
            if status == "done":
                contents[done_fn].add(str(key))
            elif status == "failed":
                contents[failed_fn].add(str(key))
                contents[failed_all_fn].add(str(key))
            elif status == "failed_all":
                contents[failed_all_fn].add(str(key))
    return contents


def write_scan_report(root: Path, index_path: Path, counts: Counter, skipped: Counter,
                      changes: list[dict], errors: list[tuple[str, str]], apply_mode: bool) -> Path:
    """Write a standalone timestamped diagnostic report without touching source status files."""
    report_dir = root / "status_repair_logs"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_path = report_dir / f"status_repair_{stamp}.txt"
    lines = [
        "IncandescenceArchiver 状态清洗诊断报告",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"模式：{'apply（将执行写回）' if apply_mode else 'scan（只读预览）'}",
        f"状态主索引：{index_path}",
        f"本地检查根目录：{root}",
        "",
        "一、按类型统计（总条目 / 目标状态）",
    ]
    for kind in KINDS:
        lines.append(
            f"{kind}: 总数={counts[(kind, 'total')]}，"
            f"done={counts[(kind, 'done')]}，failed={counts[(kind, 'failed')]}，"
            f"failed_all={counts[(kind, 'failed_all')]}，无法判定/跳过={skipped[kind]}"
        )
    lines += ["", f"二、需要变更的状态：{len(changes)} 条", ""]
    if not changes:
        lines.append("没有发现需要变更的状态。")
    for i, c in enumerate(changes, 1):
        lines += [
            f"[{i}] 类型：{c['kind']}",
            f"    键：{c['key']}",
            f"    原状态：{c['old']!r}",
            f"    目标状态：{c['new']}",
            f"    判定原因：{c['reason']}",
            "",
        ]
    lines += ["三、JSON 读取/解析或资源 URL 映射错误（对应 media 状态保留原值）", ""]
    if errors:
        for name, err in errors:
            lines.append(f"JSON：{name}")
            lines.append(f"错误：{err}")
            lines.append("")
    else:
        lines.append("无。")
    lines += ["四、说明", "目标状态根据本地文件是否存在且非空计算。图片 basename、视频 media key、头像 PID 的提取规则与 archive.py 对照。无法将 JSON 中资源 URL 映射到本地 key 时，不把 media 误判为 done，而是保留原状态并记录原因。此报告不会修改原始 HTML、JSON 或媒体文件。"]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def run(apply: bool) -> int:
    root = locate_root()
    log_dir = root / "_log"
    index_path = log_dir / "archive_index.json"
    if not index_path.is_file():
        raise RuntimeError(f"主状态文件不存在：{index_path}")
    index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    if not isinstance(index, dict) or "schema_version" not in index:
        raise RuntimeError("archive_index.json 结构不符合预期；未做任何修改。")
    for kind in KINDS:
        if kind not in index or not isinstance(index[kind], dict):
            raise RuntimeError(f"archive_index.json 缺少有效的 {kind} 对象；未做任何修改。")

    maps = compute_file_maps(root)
    json_cache = {}
    errors = []
    changes = []
    skipped = Counter()
    counts = Counter()
    for kind in KINDS:
        entries = index[kind]
        for key, rec in entries.items():
            counts[(kind, "total")] += 1
            if not isinstance(rec, dict):
                skipped[kind] += 1
                continue
            old = rec.get("status")
            new, reason = desired_status(kind, str(key), rec, root, maps, json_cache, errors)
            if new is None:
                skipped[kind] += 1
                continue
            counts[(kind, new)] += 1
            if old != new:
                changes.append({"kind": kind, "key": str(key), "old": old, "new": new, "reason": reason})

    print(f"状态文件：{index_path}")
    print(f"本地检查根目录：{root}")
    print("\n按类型统计（总条目 / 目标状态）：")
    for kind in KINDS:
        total = counts[(kind, "total")]
        print(f"  {kind:<8} {total:>5} 条；done={counts[(kind,'done')]} failed={counts[(kind,'failed')]} failed_all={counts[(kind,'failed_all')]}；无法判定/跳过={skipped[kind]}")
    print(f"\n状态需要变更：{len(changes)} 条")
    per_kind = Counter(c["kind"] for c in changes)
    for kind in KINDS:
        if per_kind[kind]:
            print(f"  {kind}: {per_kind[kind]} 条")
    if errors:
        print(f"JSON 读取/解析失败：{len(set(k for k, _ in errors))} 份；相关 media 状态保留原值")
        for name, err in errors[:8]:
            print(f"  {name}: {err}")
    report_path = write_scan_report(root, index_path, counts, skipped, changes, errors, apply)
    print(f"\n详细诊断日志：{report_path}")
    for c in changes[:12]:
        print(f"  [{c['kind']}] {c['old']} -> {c['new']} | {c['key']} | {c['reason']}")
    if len(changes) > 12:
        print(f"  ……其余 {len(changes)-12} 条省略")

    if not apply:
        print("\n这是只读预览，没有修改任何文件。确认结果后运行：python ..\\..\\status_cleaner.py apply")
        return 0

    if not changes:
        print("\n没有状态需要修改；仍会按现有状态重写 .txt 清单。")
    backup_dir = backup_files(log_dir, index_path)
    changed_by_kind = Counter()
    for c in changes:
        index[c["kind"]][c["key"]]["status"] = c["new"]
        changed_by_kind[c["kind"]] += 1
    # Strictly preserve all other index fields, including last_updated/reasons/dependencies.
    tmp = index_path.with_suffix(index_path.suffix + ".status-cleaner.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(index_path)

    txt_contents = build_txt_contents(index)
    for fn, lines in txt_contents.items():
        p = log_dir / fn
        p.write_text("".join(line + "\n" for line in sorted(lines)), encoding="utf-8")
    print(f"\n已应用状态修正。备份位置：{backup_dir}")
    print("已重生成 15 个状态 TXT；未改动 HTML、JSON、图片、视频、头像及其他索引字段。")
    print("各类型实际变更：" + (", ".join(f"{k}={changed_by_kind[k]}" for k in KINDS) if changes else "0 条"))
    return 0


def main():
    parser = argparse.ArgumentParser(description="按本地文件真实存在情况修正 IncandescenceArchiver 状态")
    parser.add_argument("command", choices=("scan", "apply"), help="scan=只读预览；apply=备份后写回状态并重生成 TXT")
    args = parser.parse_args()
    try:
        return run(apply=args.command == "apply")
    except Exception as e:
        print(f"错误：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
