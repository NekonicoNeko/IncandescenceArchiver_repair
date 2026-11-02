#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 archive.py 对媒体文件名的优先级：优先选 fetch-media 的点号命名。

放到 archive.py 同目录执行：
    python apply_render_media_priority_fix.py

修复 6 个排序位置，分别服务于 clean-html、build-index、render-html。
不修改媒体文件名，也不修改已有 HTML / index.json。自动备份 archive.py。
标准库即可运行。
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import sys
import tempfile

REPLACEMENTS = (
    ('0 if "_pbs_twimg_com_" in fname else 1', '0 if "_pbs.twimg.com_" in fname else 1', 1),
    ('0 if "_video_twimg_com_" in fname else 1', '0 if "_video.twimg.com_" in fname else 1', 1),
    ('0 if "_pbs_twimg_com_" in f else 1', '0 if "_pbs.twimg.com_" in f else 1', 2),
    ('0 if "_video_twimg_com_" in f else 1', '0 if "_video.twimg.com_" in f else 1', 2),
)


def main() -> int:
    target = Path(__file__).resolve().parent / 'archive.py'
    if not target.is_file() or target.is_symlink():
        print(f'[停止] 找不到普通文件 archive.py：{target}')
        return 2

    raw = target.read_bytes()
    try:
        source = raw.decode('utf-8-sig')
    except UnicodeError:
        print('[停止] archive.py 不是 UTF-8 编码，未修改。')
        return 2
    updated = source
    changed_count = 0
    for old, new, expected_count in REPLACEMENTS:
        existing_old = updated.count(old)
        existing_new = updated.count(new)
        if existing_old == expected_count and existing_new == 0:
            updated = updated.replace(old, new)
            changed_count += existing_old
        elif existing_old == 0 and existing_new == expected_count:
            continue  # 该处已经打过补丁
        else:
            print(f'[停止] 检测到与预期不一致或只改了一半的代码：{old!r}')
            print(f'原写法 {existing_old} 处，新写法 {existing_new} 处；archive.py 未修改。')
            return 2
    if not changed_count:
        print('媒体优先级补丁已存在，不需要再次修改。')
        return 0
    # 校验所有 6 个位置的优先级均已更正，不能留下半补丁。
    for old, new, expected_count in REPLACEMENTS:
        if updated.count(old) != 0 or updated.count(new) != expected_count:
            print('[停止] 补丁验证失败，未修改 archive.py。')
            return 2
    compile(updated, str(target), 'exec')
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup = target.with_name(f'archive.py.before_media_priority_fix.{timestamp}.bak')
    # 保留 BOM / 原始换行 / 原文件权限。
    encoded = (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'') + updated.encode('utf-8')
    tmp_name = None
    try:
        with backup.open('xb') as f:
            f.write(raw)
        with tempfile.NamedTemporaryFile(prefix='.archive_media_priority_', suffix='.tmp', dir=target.parent,
                                         delete=False) as f:
            tmp_name = Path(f.name)
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(target, tmp_name)
        os.replace(tmp_name, target)
    finally:
        if tmp_name is not None and tmp_name.exists():
            tmp_name.unlink()
    print(f'[完成] 统一了 {changed_count} 处排序优先级：clean-html / build-index / render-html。')
    print(f'[原文件备份] {backup}')
    print('已有 HTML 和 index.json 不会自动重写；旧文件修复脚本负责旧引用。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, SyntaxError, KeyboardInterrupt) as exc:
        print(f'[中止] {type(exc).__name__}: {exc}')
        sys.exit(2)
