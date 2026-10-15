#!/usr/bin/env python3
# Standalone repair utility for IncandescenceArchiver avatar files.
# Does not read/write archive.py state files or modify archive_index.json.
from __future__ import annotations
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print('缺少 requests。请运行：python -m pip install requests')
    raise SystemExit(2)

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
PROFILE_PID_RE = re.compile(r'/profile_images/(\d+)/')
TIMESTAMP_RE = re.compile(r'^(\d{14})_')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
POOL_BASE = os.environ.get('AVATAR_POOL_BASE', 'https://twitterarchiver.github.io/home/avatars').rstrip('/')


def locate_paths():
    # Intended invocation: run from accounts/<username>, e.g. ...\accounts\shyCatOvO
    cwd = Path.cwd()
    root = cwd / 'wayback_snapshots'
    if (root / 'json').is_dir() and (root / 'avatar').is_dir():
        return root, root / 'json', root / 'avatar', root / 'avatar_repair_logs'
    # Also allow invocation from the repo root if there is exactly one account folder.
    accounts = cwd / 'accounts'
    if accounts.is_dir():
        candidates = [p / 'wayback_snapshots' for p in accounts.iterdir()
                      if p.is_dir() and (p / 'wayback_snapshots' / 'json').is_dir()]
        if len(candidates) == 1:
            root = candidates[0]
            return root, root / 'json', root / 'avatar', root / 'avatar_repair_logs'
    raise RuntimeError('找不到 wayback_snapshots/json。请在 accounts\\用户名 目录运行此脚本。')


def snapshot_timestamp(filename: str) -> str:
    m = TIMESTAMP_RE.match(filename)
    return m.group(1) if m else ''


def pid_from_url(url: str) -> str:
    m = PROFILE_PID_RE.search(url or '')
    return m.group(1) if m else ''


def collect_avatars(json_dir: Path):
    # One record per PID, preferring the newest snapshot; retain source JSON list.
    by_pid = {}
    malformed = []
    json_files = sorted(json_dir.glob('*.json'))
    for jp in json_files:
        try:
            with jp.open('r', encoding='utf-8-sig') as f:
                data = json.load(f)
        except Exception as e:
            malformed.append({'json': jp.name, 'error': f'{type(e).__name__}: {e}'})
            continue
        users = ((data.get('includes') or {}).get('users') or []) if isinstance(data, dict) else []
        ts = snapshot_timestamp(jp.name)
        for user in users:
            if not isinstance(user, dict):
                continue
            url = str(user.get('profile_image_url') or '').strip()
            pid = pid_from_url(url)
            if not pid or not url:
                continue
            entry = {
                'pid': pid, 'url': url,
                'name': str(user.get('name') or '').strip(),
                'username': str(user.get('username') or '').strip(),
                'snapshot_ts': ts, 'source_json': jp.name,
                'source_jsons': [],
            }
            if pid not in by_pid:
                by_pid[pid] = entry
            else:
                by_pid[pid]['source_jsons'].append(jp.name)
                old_ts = by_pid[pid].get('snapshot_ts') or ''
                if ts > old_ts:
                    source_jsons = by_pid[pid].get('source_jsons', [])
                    by_pid[pid] = entry
                    by_pid[pid]['source_jsons'] = source_jsons
    for entry in by_pid.values():
        # Deduplicate source list and include chosen source.
        entry['source_jsons'] = sorted(set(entry.get('source_jsons', []) + [entry['source_json']]))
    return by_pid, json_files, malformed


def local_avatar_files(avatar_dir: Path, pid: str):
    return [p for p in avatar_dir.glob(f'avatar_{pid}.*')
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 0]


def missing_records(by_pid, avatar_dir: Path):
    out = []
    for pid, rec in sorted(by_pid.items(), key=lambda x: int(x[0])):
        files = local_avatar_files(avatar_dir, pid)
        row = dict(rec)
        row['local_exists'] = bool(files)
        row['local_files'] = ';'.join(p.name for p in files)
        if not files:
            out.append(row)
    return out


def ensure_log_dir(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data):
    with path.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_csv(path: Path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for row in rows:
            clean = dict(row)
            for k, v in list(clean.items()):
                if isinstance(v, list):
                    clean[k] = ';'.join(map(str, v))
            w.writerow(clean)


def scan(args):
    root, json_dir, avatar_dir, log_dir = locate_paths()
    ensure_log_dir(log_dir)
    by_pid, json_files, malformed = collect_avatars(json_dir)
    missing = missing_records(by_pid, avatar_dir)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'account_dir': str(Path.cwd()),
        'json_dir': str(json_dir), 'avatar_dir': str(avatar_dir),
        'json_files_scanned': len(json_files), 'unique_pids': len(by_pid),
        'existing_pid_files': len(by_pid) - len(missing), 'missing_pid_files': len(missing),
        'malformed_json_count': len(malformed), 'missing': missing, 'malformed_json': malformed,
        'note': '独立报告；未读取或修改 archive_index.json、avatar_done/failed 清单。'
    }
    report_path = log_dir / f'scan_{stamp}.json'
    csv_path = log_dir / f'missing_{stamp}.csv'
    write_json(report_path, report)
    write_csv(csv_path, missing, ['pid','username','name','url','snapshot_ts','source_json','source_jsons','local_exists','local_files'])
    print(f'扫描 JSON：{len(json_files)} 份')
    print(f'唯一头像 PID：{len(by_pid)}')
    print(f'对应本地文件存在：{len(by_pid)-len(missing)}')
    print(f'对应本地文件缺失：{len(missing)}')
    print(f'无法读取的 JSON：{len(malformed)}')
    print(f'独立报告：{report_path}')
    print(f'缺失清单 CSV：{csv_path}')
    return 0


def extension_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in IMAGE_EXTS:
        if path.endswith(ext):
            return ext
    return '.jpg'


def candidate_urls(rec):
    original = rec['url']
    ts = rec.get('snapshot_ts') or ''
    pid = rec['pid']
    ext = extension_from_url(original)
    candidates = []
    def add(url):
        if url and url not in candidates:
            candidates.append(url)
    if POOL_BASE:
        add(f'{POOL_BASE}/avatar_{pid}{ext}')
        if ext != '.jpg':
            add(f'{POOL_BASE}/avatar_{pid}.jpg')
    if '_normal.' in original:
        add(original.replace('_normal.', '_400x400.'))
        add(original.replace('_normal.', '_bigger.'))
    add(original)
    if ts:
        if '_normal.' in original:
            add(f'https://web.archive.org/web/{ts}im_/{original.replace("_normal.", "_400x400.")}')
        add(f'https://web.archive.org/web/{ts}im_/{original}')
        add(f'https://web.archive.org/web/{ts}if_/{original}')
    return candidates


def image_signature_ok(content: bytes, content_type: str) -> bool:
    # Reject Wayback error pages and HTML responses before saving them as image files.
    if not content or content.lstrip().lower().startswith((b'<!doctype html', b'<html')):
        return False
    signatures = (
        content.startswith(b'\xff\xd8\xff'),
        content.startswith(b'\x89PNG\r\n\x1a\n'),
        content.startswith((b'GIF87a', b'GIF89a')),
        content.startswith(b'RIFF') and content[8:12] == b'WEBP',
    )
    if any(signatures):
        return True
    # Some image responses may use uncommon encodings; only accept image content-type as fallback.
    return content_type.lower().startswith('image/') and len(content) > 500


def download_one(rec, avatar_dir: Path, timeout=25):
    pid = rec['pid']
    existing = local_avatar_files(avatar_dir, pid)
    if existing:
        return {'pid': pid, 'status': 'already_exists', 'file': ';'.join(p.name for p in existing), 'url_used': '', 'error': ''}
    errors = []
    headers = {'User-Agent': USER_AGENT, 'Referer': 'https://web.archive.org/'}
    for url in candidate_urls(rec):
        tmp = None
        try:
            response = requests.get(url, headers=headers, timeout=timeout, stream=False)
            response.raise_for_status()
            content = response.content
            ctype = response.headers.get('Content-Type', '')
            if not image_signature_ok(content, ctype):
                raise ValueError(f'响应不是可识别的图片 (Content-Type={ctype}, bytes={len(content)})')
            # Use real signature where possible, not just URL extension.
            if content.startswith(b'\x89PNG\r\n\x1a\n'):
                ext = '.png'
            elif content.startswith((b'GIF87a', b'GIF89a')):
                ext = '.gif'
            elif content.startswith(b'RIFF') and content[8:12] == b'WEBP':
                ext = '.webp'
            elif content.startswith(b'\xff\xd8\xff'):
                ext = '.jpg'
            else:
                ext = extension_from_url(url)
            target = avatar_dir / f'avatar_{pid}{ext}'
            tmp = avatar_dir / f'.avatar_{pid}.repair.tmp'
            with tmp.open('wb') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            return {'pid': pid, 'status': 'downloaded', 'file': target.name, 'url_used': url, 'error': ''}
        except Exception as e:
            if tmp and tmp.exists():
                try: tmp.unlink()
                except OSError: pass
            errors.append(f'{url} -> {type(e).__name__}: {e}')
    return {'pid': pid, 'status': 'failed', 'file': '', 'url_used': '', 'error': ' || '.join(errors)}


def download(args):
    root, json_dir, avatar_dir, log_dir = locate_paths()
    ensure_log_dir(log_dir)
    avatar_dir.mkdir(parents=True, exist_ok=True)
    by_pid, json_files, malformed = collect_avatars(json_dir)
    missing = missing_records(by_pid, avatar_dir)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    initial_report = log_dir / f'download_plan_{stamp}.json'
    write_json(initial_report, {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'json_files_scanned': len(json_files), 'unique_pids': len(by_pid),
        'missing_before_download': len(missing), 'malformed_json_count': len(malformed),
        'items': missing,
        'note': '只写入 avatar/avatar_<PID>.<ext> 缺失的头像；不修改原工具状态文件。'
    })
    print(f'扫描到 {len(by_pid)} 个唯一 PID；本地缺失 {len(missing)} 个。')
    print(f'独立计划日志：{initial_report}')
    if not missing:
        print('没有缺失的头像文件，无需下载。')
        return 0
    results = []
    for i, rec in enumerate(missing, 1):
        print(f'[{i}/{len(missing)}] {rec.get("username") or rec.get("name") or rec["pid"]} (PID {rec["pid"]}) ...', flush=True)
        result = download_one(rec, avatar_dir)
        result['username'] = rec.get('username', '')
        result['name'] = rec.get('name', '')
        result['source_json'] = rec.get('source_json', '')
        results.append(result)
        if result['status'] == 'downloaded':
            print(f'  成功：{result["file"]} <- {result["url_used"]}')
        elif result['status'] == 'already_exists':
            print(f'  已存在，跳过：{result["file"]}')
        else:
            print(f'  失败：所有候选地址均未成功；详情见独立日志。')
        time.sleep(0.15)
    log_path = log_dir / f'download_results_{stamp}.json'
    csv_path = log_dir / f'download_results_{stamp}.csv'
    write_json(log_path, {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'total_attempted': len(results),
        'downloaded': sum(r['status'] == 'downloaded' for r in results),
        'already_exists': sum(r['status'] == 'already_exists' for r in results),
        'failed': sum(r['status'] == 'failed' for r in results),
        'results': results,
        'malformed_json': malformed,
        'note': '独立日志；原工具 _log 与 archive_index.json 未被修改。'
    })
    write_csv(csv_path, results, ['pid','username','name','status','file','url_used','error','source_json'])
    print('\n下载完成：')
    print(f'  新下载：{sum(r["status"] == "downloaded" for r in results)}')
    print(f'  已存在：{sum(r["status"] == "already_exists" for r in results)}')
    print(f'  失败：{sum(r["status"] == "failed" for r in results)}')
    print(f'独立结果日志：{log_path}')
    print(f'结果 CSV：{csv_path}')
    return 0 if all(r['status'] != 'failed' for r in results) else 1


def main():
    parser = argparse.ArgumentParser(description='独立的 IncandescenceArchiver 头像缺失扫描/补下载工具')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('scan', help='只扫描 JSON 中引用的头像 PID，并报告本地缺失文件')
    sub.add_parser('download', help='扫描后只下载本地缺失的头像文件')
    args = parser.parse_args()
    try:
        if args.command == 'scan':
            return scan(args)
        return download(args)
    except Exception as e:
        print(f'错误：{type(e).__name__}: {e}', file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
