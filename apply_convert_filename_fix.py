#!/usr/bin/env python3
"""修复 IncandescenceArchiver archive.py convert 媒体命名规则；自动备份。"""
from pathlib import Path
import sys

OLD = 'def _media_safe_local_name(media_url: str, ts: str, ext: str) -> str:\n    """\n    给从 dump 复制过来的媒体生成本项目格式的本地文件名。\n    遵循 safe_filename 规则：{ts}_{URL前缀清洗}{ext}\n    """\n    if not ts:\n        ts = "00000000000000"\n    return safe_filename(ts, media_url, ext)\n'
NEW = 'def _media_safe_local_name(media_url: str, ts: str, ext: str) -> str:\n    """给 convert 导入的媒体生成与 fetch-media 一致的文件名。"""\n    if not ts:\n        ts = "00000000000000"\n    parsed = urlparse(media_url)\n    fname_base = re.sub(r"[^\\w\\-_.]", "_", (parsed.netloc + parsed.path).strip("/"))[:120]\n    if ext.lower() == ".mp4":\n        if not fname_base.lower().endswith(".mp4"):\n            fname_base += ".mp4"\n    elif not fname_base.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):\n        fname_base += ext_from_url(media_url)\n    return f"{ts}_{fname_base}"\n'

def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "archive.py"
    if not target.is_file():
        raise SystemExit(f"找不到 archive.py：{target}")
    with target.open("r", encoding="utf-8", newline="") as f:
        source = f.read()
    newline = "\r\n" if "\r\n" in source else "\n"
    old_block = OLD.replace("\n", newline)
    new_block = NEW.replace("\n", newline)
    if new_block in source:
        print("已经应用过此补丁，无需重复修改。")
        return
    if source.count(old_block) != 1:
        raise SystemExit("原函数与已核对的源码不一致，已停止修改。请先检查 archive.py 版本。")
    backup = target.with_name(target.name + ".before_convert_name_fix.bak")
    if backup.exists():
        raise SystemExit(f"备份文件已存在：{backup}；为避免覆盖备份，已停止修改。")
    with backup.open("w", encoding="utf-8", newline="") as f:
        f.write(source)
    changed = source.replace(old_block, new_block, 1)
    compile(changed, str(target), "exec")
    with target.open("w", encoding="utf-8", newline="") as f:
        f.write(changed)
    print(f"修复完成：{target}")
    print(f"原文件备份：{backup}")

if __name__ == "__main__":
    main()
