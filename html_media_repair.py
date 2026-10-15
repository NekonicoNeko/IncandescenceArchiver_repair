#!/usr/bin/env python3
"""Restore missing local image tags in IncandescenceArchiver HTML from JSON + local image files.

Run from accounts/<USERNAME> (or pass --root):
    python ..\\..\\html_media_repair.py scan
    python ..\\..\\html_media_repair.py apply

Only inserts <img class="tweet-image" src="../image/..."/> tags when:
- the matching JSON explicitly references the image;
- a matching, non-empty local image file exists; and
- the corresponding HTML tweet-content block does not already reference that image.
Existing HTML is otherwise preserved byte-for-byte except for inserted image tags. Missing images are inserted in JSON media order when existing tags allow an unambiguous placement; uncertain/reordered cases are skipped. Videos, avatars, JSON and indexes are untouched.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

IMAGE_EXTS = ("jpg", "jpeg", "png", "gif", "webp")
IMG_NEW_RE = re.compile(
    r"_media_([A-Za-z0-9_\-]+?)_(?:jpg|png|gif|webp|jpeg)\.(?:jpg|png|gif|webp|jpeg)$",
    re.IGNORECASE,
)
IMG_OLD_RE = re.compile(
    r"_media_([A-Za-z0-9_\-]+?)\.(?:jpg|png|gif|webp|jpeg)$",
    re.IGNORECASE,
)
IMG_URL_RE = re.compile(
    r"pbs\.twimg\.com/media/([A-Za-z0-9_\-]+?)(?:\.(?:jpg|png|gif|webp|jpeg))?$",
    re.IGNORECASE,
)
URL_MEDIA_RE = re.compile(r"/media/([^/?#]+)", re.IGNORECASE)
STATUS_RE = re.compile(r"/status/(\d+)")
QT_ID_RE = re.compile(r"^qt(\d+)$")


def image_key(value: str) -> str:
    """Mirror archive.py's basename extraction, with URL query strings ignored."""
    value = (value or "").split("?", 1)[0].split("#", 1)[0]
    for pattern in (IMG_NEW_RE, IMG_OLD_RE, IMG_URL_RE):
        match = pattern.search(value)
        if match:
            return match.group(1)
    # JSON media URLs often carry ?format=jpg&name=large; the path basename is the media key.
    match = URL_MEDIA_RE.search(value)
    if match:
        base = match.group(1)
        base = re.sub(r"\.(?:jpg|jpeg|png|gif|webp)$", "", base, flags=re.IGNORECASE)
        return base
    return ""


def local_media_index(image_dir: Path) -> dict[str, str]:
    """Map media basename to a real local file; prefer original pbs.twimg filenames."""
    result: dict[str, str] = {}
    if not image_dir.is_dir():
        return result
    files = sorted(
        (p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in {"." + e for e in IMAGE_EXTS} and p.stat().st_size > 0),
        key=lambda p: (0 if "_pbs_twimg_com_" in p.name.lower() else 1, p.name.lower()),
    )
    for path in files:
        key = image_key(path.name)
        if key:
            result.setdefault(key, path.name)
    return result


def json_tweet_media(data: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return media_key -> media, and included tweet_id -> tweet."""
    includes = data.get("includes") or {}
    media_by_key = {}
    for item in includes.get("media", []) or []:
        key = str(item.get("media_key") or "")
        if key:
            media_by_key[key] = item
    tweets_by_id = {}
    for item in includes.get("tweets", []) or []:
        tid = str(item.get("id") or "")
        if tid:
            tweets_by_id[tid] = item
    return media_by_key, tweets_by_id


def image_keys_for_tweet(tweet: dict, media_by_key: dict[str, dict]) -> list[str]:
    keys = []
    attachments = tweet.get("attachments") or {}
    for media_key in attachments.get("media_keys", []) or []:
        media = media_by_key.get(str(media_key))
        if not media or media.get("type") != "photo":
            continue
        url = media.get("url") or media.get("preview_image_url") or ""
        key = image_key(url)
        if key and key not in keys:
            keys.append(key)
    return keys


def absolute_offset(text: str, line_offsets: list[int], line: int, col: int) -> int:
    return line_offsets[line - 1] + col


class DivNode:
    def __init__(self, attrs: dict[str, str | None], start: int, parent: "DivNode | None"):
        self.attrs = attrs
        self.start = start
        self.end: int | None = None
        self.parent = parent
        self.children: list[DivNode] = []
        self.images: list[str] = []
        self.image_refs: list[tuple[str, int]] = []
        self.links: list[tuple[str, str]] = []
        self.date_start: int | None = None
        if parent:
            parent.children.append(self)

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def has_ancestor_class(self, cls: str) -> bool:
        p = self.parent
        while p:
            if cls in p.classes:
                return True
            p = p.parent
        return False

    def nearest_ancestor_class(self, cls: str) -> "DivNode | None":
        p = self.parent
        while p:
            if cls in p.classes:
                return p
            p = p.parent
        return None

    def all_links(self) -> list[tuple[str, str]]:
        out = list(self.links)
        for child in self.children:
            out.extend(child.all_links())
        return out

    def scoped_images(self, root: bool = True) -> list[str]:
        """Images inside this tweet-content, excluding nested quoted tweet-content blocks."""
        return [key for key, _ in self.scoped_image_refs(root=root)]

    def scoped_image_refs(self, root: bool = True) -> list[tuple[str, int]]:
        """Image keys and source offsets, excluding nested quoted tweet-content blocks."""
        out = list(self.image_refs)
        for child in self.children:
            if "tweet-content" in child.classes:
                continue
            out.extend(child.scoped_image_refs(root=False))
        return out


class MinimalHTMLScanner(HTMLParser):
    """Find target div spans and current image refs without rewriting the HTML."""
    def __init__(self, text: str):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.line_offsets = [0]
        for match in re.finditer("\n", text):
            self.line_offsets.append(match.end())
        self.div_stack: list[DivNode] = []
        self.nodes: list[DivNode] = []
        self.feed(text)
        self.close()

    def _offset(self) -> int:
        line, col = self.getpos()
        return absolute_offset(self.text, self.line_offsets, line, col)

    def handle_starttag(self, tag: str, attrs_list):
        attrs = {k: v for k, v in attrs_list}
        offset = self._offset()
        if tag.lower() == "div":
            parent = self.div_stack[-1] if self.div_stack else None
            node = DivNode(attrs, offset, parent)
            self.nodes.append(node)
            self.div_stack.append(node)
        elif tag.lower() == "img":
            src = attrs.get("src") or ""
            key = image_key(src)
            if key:
                # Associate each image only with its nearest tweet-content.
                # Otherwise quoted-tweet images are incorrectly counted on
                # every ancestor div, including the main tweet-content.
                for node in reversed(self.div_stack):
                    if "tweet-content" in node.classes:
                        node.images.append(key)
                        node.image_refs.append((key, offset))
                        break
        elif tag.lower() == "a":
            href = attrs.get("href") or ""
            ident = attrs.get("id") or ""
            for node in self.div_stack:
                node.links.append((href, ident))
        elif tag.lower() == "p" and "date" in set((attrs.get("class") or "").split()):
            for node in reversed(self.div_stack):
                if "tweet-content" in node.classes and node.date_start is None:
                    node.date_start = offset
                    break

    def handle_startendtag(self, tag: str, attrs_list):
        self.handle_starttag(tag, attrs_list)
        if tag.lower() == "div":
            self.handle_endtag("div")

    def handle_endtag(self, tag: str):
        if tag.lower() != "div" or not self.div_stack:
            return
        node = self.div_stack.pop()
        node.end = self._offset()


def tweet_id_for_embedded(node: DivNode) -> str:
    for href, ident in node.all_links():
        match = STATUS_RE.search(href)
        if match:
            return match.group(1)
        match = QT_ID_RE.match(ident)
        if match:
            return match.group(1)
    return ""


def target_nodes(scanner: MinimalHTMLScanner) -> tuple[DivNode | None, dict[str, DivNode]]:
    contents = [n for n in scanner.nodes if "tweet-content" in n.classes and n.end is not None]
    main = None
    embedded: dict[str, DivNode] = {}
    for node in contents:
        if node.has_ancestor_class("embedded-tweet-container"):
            continue
        # Favor content inside #nonjsonview, which is the visible main tweet.
        p = node.parent
        inside_nonjson = False
        while p:
            if p.attrs.get("id") == "nonjsonview":
                inside_nonjson = True
                break
            p = p.parent
        if inside_nonjson:
            main = node
            break
    for node in contents:
        container = node.nearest_ancestor_class("embedded-tweet-container")
        if container:
            tid = tweet_id_for_embedded(container)
            if tid:
                embedded.setdefault(tid, node)
    return main, embedded


def expected_by_target(data: dict, media_by_key: dict[str, dict], tweets_by_id: dict[str, dict]):
    td = data.get("data") or {}
    result: dict[str, list[str]] = {"__main__": image_keys_for_tweet(td, media_by_key)}
    for ref in td.get("referenced_tweets", []) or []:
        tid = str(ref.get("id") or "")
        if tid and tid in tweets_by_id:
            result[tid] = image_keys_for_tweet(tweets_by_id[tid], media_by_key)
    return result


def locate_root(arg: str | None) -> Path:
    candidates = [Path(arg).expanduser().resolve()] if arg else [Path.cwd().resolve()]
    for candidate in candidates:
        if (candidate / "html").is_dir() and (candidate / "json").is_dir() and (candidate / "image").is_dir():
            return candidate
        if (candidate / "wayback_snapshots" / "html").is_dir():
            return candidate / "wayback_snapshots"
    raise FileNotFoundError(
        "找不到归档目录。请在 accounts\\用户名 目录运行，或用 --root 指定包含 html、json、image 的 wayback_snapshots 目录。"
    )


def analyze_html(html_path: Path, json_path: Path, image_index: dict[str, str]):
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        text = html_path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, f"读取失败：{exc}"

    media_by_key, tweets_by_id = json_tweet_media(data)
    expected = expected_by_target(data, media_by_key, tweets_by_id)
    scanner = MinimalHTMLScanner(text)
    main, embedded = target_nodes(scanner)
    if not main:
        return None, "找不到主推文 .tweet-content，跳过以避免误插入"

    insertions: list[tuple[int, str, str]] = []
    missing_count = 0

    targets: list[tuple[str, DivNode]] = [("__main__", main)]
    for tid, node in embedded.items():
        if tid in expected:
            targets.append((tid, node))

    for target_id, node in targets:
        expected_keys = expected.get(target_id, [])
        image_refs = node.scoped_image_refs()
        existing_keys = {key for key, _ in image_refs}
        existing_offsets: dict[str, int] = {}
        for key, offset in image_refs:
            existing_offsets.setdefault(key, offset)

        # Do not silently produce a mixed/reordered gallery if existing image tags
        # are already out of JSON order. Such files need a more specific repair.
        expected_positions = {key: i for i, key in enumerate(expected_keys)}
        present_order = [
            expected_positions[key]
            for key, _ in image_refs
            if key in expected_positions
        ]
        if any(a > b for a, b in zip(present_order, present_order[1:])):
            return None, f"HTML 现有图片顺序与 JSON 不一致，跳过以避免改变画廊顺序"

        # Insert each missing image before the next existing image in JSON order;
        # only images with no later existing neighbor are appended before the date.
        anchors: dict[int, list[str]] = {}
        for i, key in enumerate(expected_keys):
            if key in existing_keys:
                continue
            filename = image_index.get(key)
            if not filename:
                continue
            anchor = None
            for later_key in expected_keys[i + 1:]:
                if later_key in existing_offsets:
                    anchor = existing_offsets[later_key]
                    break
            if anchor is None:
                anchor = node.date_start if node.date_start is not None else node.end
            if anchor is None:
                continue
            src = "../image/" + filename
            tag = f'<img class="tweet-image" src="{html.escape(src, quote=True)}"/>'
            anchors.setdefault(anchor, []).append(tag)

        if not anchors:
            continue
        missing_count += sum(len(tags) for tags in anchors.values())
        for offset, tags in anchors.items():
            insertion = "\n" + "\n".join(tags) + "\n"
            insertions.append((offset, insertion, target_id))

    if not insertions:
        return {"text": text, "additions": [], "missing_count": 0}, None

    new_text = text
    for offset, insertion, _ in sorted(insertions, key=lambda x: x[0], reverse=True):
        new_text = new_text[:offset] + insertion + new_text[offset:]
    return {
        "text": new_text,
        "additions": insertions,
        "missing_count": missing_count,
    }, None


def main() -> int:
    parser = argparse.ArgumentParser(description="仅补回 JSON 和本地文件都存在、但 HTML 缺失的图片标签")
    parser.add_argument("mode", choices=("scan", "apply"), help="scan 只预览；apply 备份后写入")
    parser.add_argument("--root", help="wayback_snapshots 目录，或包含 wayback_snapshots 的账号目录")
    args = parser.parse_args()

    try:
        root = locate_root(args.root)
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    html_dir, json_dir, image_dir = root / "html", root / "json", root / "image"
    image_index = local_media_index(image_dir)
    html_files = sorted(html_dir.glob("*.html"))
    print(f"归档目录：{root}")
    print(f"HTML：{len(html_files)}；可匹配本地图片 key：{len(image_index)}")
    print(f"模式：{'仅扫描，不写入' if args.mode == 'scan' else '修复并备份'}")

    plans: list[tuple[Path, dict]] = []
    skipped_no_json = skipped_error = 0
    for html_path in html_files:
        json_path = json_dir / (html_path.stem + ".json")
        if not json_path.is_file():
            skipped_no_json += 1
            continue
        result, error = analyze_html(html_path, json_path, image_index)
        if error:
            skipped_error += 1
            print(f"[跳过] {html_path.name}: {error}")
            continue
        if result["missing_count"]:
            plans.append((html_path, result))
            print(f"[待补] {html_path.name}: 将补入 {result['missing_count']} 张图片")

    total_images = sum(result["missing_count"] for _, result in plans)
    print("\n扫描汇总：")
    print(f"  需要修改的 HTML：{len(plans)}")
    print(f"  待补图片标签：{total_images}")
    print(f"  缺少同名 JSON：{skipped_no_json}")
    print(f"  解析失败/结构不确定而跳过：{skipped_error}")

    if args.mode == "scan":
        print("\n这是预览，没有修改任何文件。确认数量合理后再运行 apply。")
        return 0
    if not plans:
        print("没有需要修复的 HTML。")
        return 0

    backup_dir = root / "_log" / f"html_media_repair_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    changed = 0
    for html_path, result in plans:
        relative = html_path.relative_to(html_dir)
        backup_path = backup_dir / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(html_path, backup_path)
        html_path.write_text(result["text"], encoding="utf-8", newline="")
        changed += 1
    print(f"\n修复完成：修改 {changed} 个 HTML，补入 {total_images} 张图片标签。")
    print(f"备份位置：{backup_dir}")
    print("没有修改 JSON、视频、头像、索引或其他 HTML 内容。")
    print("如果 Reader 使用 index.json，请随后运行原工具的 build-index 更新索引。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
