#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Find which archived HTML/JSON files reference a supplied media/tweet/avatar identifier.

Run from accounts/<username>/ or accounts/<username>/wayback_snapshots/:
    python find_media_refs.py <identifier>
    python find_media_refs.py <identifier> --root "G:\\工具\\IncandescenceArchiver\\accounts\\NAME\\wayback_snapshots"

Read-only: does not modify any files.
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

MEDIA_KEY_RE = re.compile(r"pbs\.twimg\.com/media/([A-Za-z0-9_-]+?)(?:\.(?:jpg|jpeg|png|gif|webp))?(?:$|[?#])", re.I)
MEDIA_FILE_NEW_RE = re.compile(r"_media_([A-Za-z0-9_-]+?)_(?:jpg|jpeg|png|gif|webp)\.(?:jpg|jpeg|png|gif|webp)$", re.I)
MEDIA_FILE_OLD_RE = re.compile(r"_media_([A-Za-z0-9_-]+?)\.(?:jpg|jpeg|png|gif|webp)$", re.I)
AVATAR_FILE_RE = re.compile(r"avatar_(\d+)\.(?:jpg|jpeg|png|gif|webp)$", re.I)
PROFILE_PID_RE = re.compile(r"/profile_images/(\d+)/", re.I)
STATUS_ID_RE = re.compile(r"/(?:status|statuses)/(\d+)", re.I)
VIDEO_KEY_RE = re.compile(r"(?:amplify_video|ext_tw_video)[/_](?:v2/)?(\d+)|tweet_video[/_]([A-Za-z0-9_-]+)", re.I)


def clean_input(value: str) -> str:
    return value.strip().strip('"\'')


def variants_for(query: str) -> list[str]:
    """Produce exact identifier variants from a basename, URL, media key, or tweet ID."""
    q = clean_input(query)
    variants: list[str] = [q]
    # If a URL was supplied, retain the URL and derive its stable ID where possible.
    for regex in (MEDIA_KEY_RE, PROFILE_PID_RE, STATUS_ID_RE):
        m = regex.search(q)
        if m:
            variants.append(m.group(1))
    vm = VIDEO_KEY_RE.search(q)
    if vm:
        variants.append(vm.group(1) or vm.group(2))
    # If a local filename was supplied, extract the stable media/avatar ID.
    name = (urlparse(q).path or q).replace("\\", "/").rsplit("/", 1)[-1]
    for regex in (MEDIA_FILE_NEW_RE, MEDIA_FILE_OLD_RE, AVATAR_FILE_RE):
        m = regex.search(name)
        if m:
            variants.append(m.group(1))
    vm = VIDEO_KEY_RE.search(name)
    if vm:
        variants.append(vm.group(1) or vm.group(2))
    # An avatar PID may be given as a bare number; a bare media key stays unchanged.
    out: list[str] = []
    seen: set[str] = set()
    for item in variants:
        item = item.strip()
        if item and item.casefold() not in seen:
            seen.add(item.casefold())
            out.append(item)
    return out


def locate_snapshot_root(root_arg: str | None) -> Path:
    if root_arg:
        root = Path(root_arg).expanduser().resolve()
        if (root / "html").is_dir() or (root / "json").is_dir():
            return root
        if (root / "wayback_snapshots").is_dir():
            return root / "wayback_snapshots"
        raise FileNotFoundError(f"指定目录下找不到 html/ 或 json/：{root}")

    cwd = Path.cwd().resolve()
    candidates = [cwd, cwd / "wayback_snapshots", cwd.parent / "wayback_snapshots"]
    for root in candidates:
        if (root / "html").is_dir() or (root / "json").is_dir():
            return root
    raise FileNotFoundError(
        "当前目录附近找不到 html/ 或 json/。请在账号目录或 wayback_snapshots 目录运行，"
        "也可以用 --root 指定目录。"
    )


def search_files(folder: Path, kind: str, variants: list[str], root: Path) -> list[tuple[Path, list[str], str]]:
    found: list[tuple[Path, list[str], str]] = []
    if not folder.is_dir():
        return found
    # Longest first prevents a shorter derived token from hiding a more informative match.
    needles = sorted(variants, key=len, reverse=True)
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() != f".{kind}":
            continue
        matched: list[str] = []
        # File names matter too: tweet IDs are often in the archive filename, not the HTML body.
        filename = path.name.casefold()
        for needle in needles:
            if needle.casefold() in filename:
                matched.append(needle)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        folded = text.casefold()
        for needle in needles:
            if needle.casefold() in folded and needle not in matched:
                matched.append(needle)
        if matched:
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            found.append((path, matched, rel))
    return sorted(found, key=lambda x: x[2].casefold())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读检索归档 HTML/JSON：输入 avatar PID、media key/basename、推文 ID 或原始 URL。"
    )
    parser.add_argument("identifier", help="例如 avatar_123456.jpg、Gh-iuyMbUAAeEwh、推文 ID 或完整 URL")
    parser.add_argument("--root", help="wayback_snapshots 目录，或包含该目录的账号目录")
    args = parser.parse_args()

    try:
        root = locate_snapshot_root(args.root)
    except FileNotFoundError as exc:
        print(f"错误：{exc}")
        return 2

    variants = variants_for(args.identifier)
    html_hits = search_files(root / "html", "html", variants, root)
    json_hits = search_files(root / "json", "json", variants, root)

    print(f"检索标识：{args.identifier}")
    if len(variants) > 1:
        print("同时匹配的标识：" + "、".join(variants))
    print(f"归档目录：{root}")
    print()

    if html_hits:
        print(f"HTML 命中：{len(html_hits)} 个文件")
        for _, matched, rel in html_hits:
            print(f"  [HTML] {rel}  (匹配：{', '.join(matched)})")
    else:
        print("HTML：没有找到引用该标识的文件。")

    print()
    if json_hits:
        print(f"JSON 命中：{len(json_hits)} 个文件")
        for _, matched, rel in json_hits:
            print(f"  [JSON] {rel}  (匹配：{', '.join(matched)})")
    else:
        print("JSON：没有找到引用该标识的文件。")

    print()
    if not html_hits and not json_hits:
        print("结果：HTML 和 JSON 中都没有找到该标识。")
    else:
        print("结果：检索完成。此工具只读取文件，不会修改归档内容。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
