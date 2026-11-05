#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IncandescenceArchiver unified media repair tool.

Run from accounts/<username>:
    python ../../media_repair.py              # avatar -> imgvid -> status -> html
    python ../../media_repair.py avatar       # download missing avatars
    python ../../media_repair.py avatar --scan
    python ../../media_repair.py imgvid       # download missing images/videos
    python ../../media_repair.py imgvid --scan
    python ../../media_repair.py imgvid --type images
    python ../../media_repair.py status       # apply status corrections
    python ../../media_repair.py status --scan
    python ../../media_repair.py html         # apply HTML image repair
    python ../../media_repair.py html --scan

The original four implementations are packaged intact in this single file,
with their separate module namespaces to avoid collisions between helpers.
Only the avatar/status report directory names were unified. Existing HTML
backups and status backups retain their original locations under _log.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
import traceback
import types
from datetime import datetime
from pathlib import Path

ORDER = ('avatar', 'imgvid', 'status', 'html')

# ===== avatar_repair.py: original implementation (embedded) =====
_SOURCE_AVATAR = r'''
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
        return root, root / 'json', root / 'avatar', root / 'media_repair_logs'
    # Also allow invocation from the repo root if there is exactly one account folder.
    accounts = cwd / 'accounts'
    if accounts.is_dir():
        candidates = [p / 'wayback_snapshots' for p in accounts.iterdir()
                      if p.is_dir() and (p / 'wayback_snapshots' / 'json').is_dir()]
        if len(candidates) == 1:
            root = candidates[0]
            return root, root / 'json', root / 'avatar', root / 'media_repair_logs'
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
            # Match archive.py: avatar filename extension follows the original profile URL.
            # The actual image signature is still validated above before saving.
            ext = extension_from_url(rec['url'])
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
'''

# ===== imgvid_repair.py: original implementation (embedded) =====
_SOURCE_IMGVID = r'''
#!/usr/bin/env python3
"""Standalone image/video repair utility for IncandescenceArchiver archives.

Scans tweet JSON references and checks actual local files, ignoring archive.py state files.
Only writes missing image/video files and independent logs under wayback_snapshots/media_repair_logs.
"""
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
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import requests
except ImportError:
    print('缺少 requests。请运行：python -m pip install requests')
    raise SystemExit(2)

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
IMAGE_BASENAME_RE = re.compile(r'pbs\.twimg\.com/media/([A-Za-z0-9_\-]+?)(?:\.(?:jpg|png|gif|webp|jpeg))?(?:$|\?)', re.I)
IMAGE_FILENAME_NEW_RE = re.compile(r'_media_([A-Za-z0-9_\-]+?)_(?:jpg|png|gif|webp|jpeg)\.(?:jpg|png|gif|webp|jpeg)$', re.I)
IMAGE_FILENAME_OLD_RE = re.compile(r'_media_([A-Za-z0-9_\-]+?)\.(?:jpg|png|gif|webp|jpeg)$', re.I)
VIDEO_KEY_RE = re.compile(r'(?:amplify_video|ext_tw_video)[/_](\d+)|tweet_video[/_]([A-Za-z0-9_-]+)', re.I)
TIMESTAMP_RE = re.compile(r'^(\d{14})_')
PROXY_BASE = os.environ.get('TWITTER_PROXY_BASE', '').rstrip('/')


def locate_paths():
    cwd = Path.cwd()
    root = cwd / 'wayback_snapshots'
    if (root / 'json').is_dir() and (root / 'image').is_dir() and (root / 'video').is_dir():
        return root, root / 'json', root / 'image', root / 'video', root / 'media_repair_logs'
    accounts = cwd / 'accounts'
    if accounts.is_dir():
        candidates = [p / 'wayback_snapshots' for p in accounts.iterdir()
                      if p.is_dir() and (p / 'wayback_snapshots' / 'json').is_dir()]
        if len(candidates) == 1:
            root = candidates[0]
            return root, root / 'json', root / 'image', root / 'video', root / 'media_repair_logs'
    raise RuntimeError('找不到 wayback_snapshots/json。请在 accounts\\用户名 目录运行此脚本。')


def ts_from_filename(name):
    m = TIMESTAMP_RE.match(name)
    return m.group(1) if m else ''


def image_basename(value):
    no_query = (value or '').split('?', 1)[0]
    m = IMAGE_FILENAME_NEW_RE.search(no_query) or IMAGE_FILENAME_OLD_RE.search(no_query)
    if m:
        return m.group(1)
    m = IMAGE_BASENAME_RE.search(value or '')
    return m.group(1) if m else ''


def video_key(value):
    m = VIDEO_KEY_RE.search(value or '')
    return (m.group(1) or m.group(2)) if m else ''


def safe_media_filename(url, timestamp, kind):
    parsed = urlparse(url)
    base = re.sub(r'[^\w\-_.]', '_', (parsed.netloc + parsed.path).strip('/'))[:120]
    if kind == 'image':
        if not base.lower().endswith(IMAGE_EXTS):
            base += ext_from_url(url)
    else:
        if not base.lower().endswith('.mp4'):
            base += '.mp4'
    return f'{timestamp or "00000000000000"}_{base}'


def ext_from_url(url):
    path = urlparse(url).path.lower()
    for ext in ('.png', '.gif', '.webp', '.jpeg', '.jpg'):
        if path.endswith(ext):
            return ext
    query = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    fmt = query.get('format', '').lower()
    return f'.{fmt}' if fmt in ('png', 'gif', 'webp', 'jpeg', 'jpg') else '.jpg'


def proxy_url(url):
    if not PROXY_BASE:
        return url
    p = urlparse(url)
    if p.netloc in ('video.twimg.com', 'pbs.twimg.com', 'abs.twimg.com'):
        return f'{PROXY_BASE}/{p.netloc}{p.path}' + (f'?{p.query}' if p.query else '')
    return url


def image_candidates(url, timestamp):
    out, seen = [], set()
    def add(u):
        if u and u not in seen:
            seen.add(u); out.append(u)
    p = urlparse(url)
    if p.netloc.endswith('pbs.twimg.com') and p.path.startswith('/media/'):
        query = dict(parse_qsl(p.query, keep_blank_values=True))
        suffix = os.path.splitext(p.path)[1].lower()
        fmt = query.get('format', '')
        norm_path = p.path
        if suffix in IMAGE_EXTS:
            fmt = fmt or suffix.lstrip('.')
            norm_path = p.path[:-len(suffix)]
            add(proxy_url(url))
        elif fmt:
            add(proxy_url(urlunparse(p._replace(path=norm_path + '.' + fmt, query=''))))
        if fmt:
            for name in ('orig', '4096x4096', 'large', 'medium', 'small', '900x900'):
                q = dict(query); q['format'] = fmt; q['name'] = name
                add(proxy_url(urlunparse(p._replace(path=norm_path, query=urlencode(q)))))
        add(proxy_url(url)); add(proxy_url(urlunparse(p._replace(query=''))))
    else:
        add(proxy_url(url))
        if p.query: add(proxy_url(urlunparse(p._replace(query=''))))
    if timestamp:
        add(f'https://web.archive.org/web/{timestamp}im_/{url}')
        add(f'https://web.archive.org/web/{timestamp}if_/{url}')
        add(f'https://web.archive.org/web/{timestamp}/{url}')
    return out


def video_candidates(url, timestamp):
    out, seen = [], set()
    def add(u):
        if u and u not in seen:
            seen.add(u); out.append(u)
    add(proxy_url(url))
    p = urlparse(url)
    if p.netloc.endswith('video.twimg.com') and p.query:
        add(proxy_url(urlunparse(p._replace(query=''))))
    if timestamp:
        add(f'https://web.archive.org/web/{timestamp}im_/{url}')
        add(f'https://web.archive.org/web/{timestamp}if_/{url}')
        add(f'https://web.archive.org/web/{timestamp}/{url}')
    return out


def collect_media(json_dir):
    images, videos, malformed = {}, {}, []
    json_files = sorted(json_dir.glob('*.json'))
    for jp in json_files:
        try:
            with jp.open('r', encoding='utf-8-sig') as f:
                data = json.load(f)
        except Exception as e:
            malformed.append({'json': jp.name, 'error': f'{type(e).__name__}: {e}'})
            continue
        if not isinstance(data, dict):
            continue
        includes = data.get('includes') or {}
        snapshot_ts = ts_from_filename(jp.name)
        for media in includes.get('media') or []:
            if not isinstance(media, dict):
                continue
            mtype = media.get('type', '')
            if mtype == 'photo':
                url = str(media.get('url') or '').strip()
                key = image_basename(url)
                if not url or not key:
                    continue
                rec = {'kind': 'image', 'key': key, 'url': url, 'snapshot_ts': snapshot_ts,
                       'source_json': jp.name, 'source_jsons': []}
                old = images.get(key)
                if old is None:
                    images[key] = rec
                else:
                    old['source_jsons'].append(jp.name)
                    if snapshot_ts > old.get('snapshot_ts', ''):
                        refs = old['source_jsons']
                        images[key] = rec; images[key]['source_jsons'] = refs
            elif mtype in ('video', 'animated_gif'):
                variants = media.get('variants') or []
                candidates = [v for v in variants if isinstance(v, dict) and v.get('content_type') == 'video/mp4' and v.get('url')]
                if not candidates:
                    continue
                best = max(candidates, key=lambda v: int(v.get('bit_rate') or 0))
                url = str(best.get('url') or '').strip()
                key = video_key(url)
                if not url or not key:
                    continue
                rec = {'kind': 'video', 'key': key, 'url': url, 'snapshot_ts': snapshot_ts,
                       'source_json': jp.name, 'source_jsons': []}
                old = videos.get(key)
                if old is None:
                    videos[key] = rec
                else:
                    old['source_jsons'].append(jp.name)
                    if snapshot_ts > old.get('snapshot_ts', ''):
                        refs = old['source_jsons']
                        videos[key] = rec; videos[key]['source_jsons'] = refs
    for group in (images, videos):
        for rec in group.values():
            rec['source_jsons'] = sorted(set(rec.get('source_jsons', []) + [rec['source_json']]))
    return images, videos, json_files, malformed


def local_image_files(image_dir):
    out = {}
    if image_dir.exists():
        for p in image_dir.iterdir():
            if not p.is_file() or p.stat().st_size <= 0:
                continue
            key = image_basename(p.name)
            if key:
                out.setdefault(key, []).append(p)
    return out


def local_video_files(video_dir):
    out = {}
    if video_dir.exists():
        for p in video_dir.iterdir():
            if not p.is_file() or p.stat().st_size <= 0:
                continue
            key = video_key(p.name)
            if key:
                out.setdefault(key, []).append(p)
    return out


def get_missing(images, videos, image_dir, video_dir, which='all'):
    img_local = local_image_files(image_dir)
    vid_local = local_video_files(video_dir)
    missing = []
    if which in ('all', 'images'):
        for key, rec in sorted(images.items()):
            files = img_local.get(key, [])
            if not files:
                row = dict(rec); row['local_files'] = ''; missing.append(row)
    if which in ('all', 'videos'):
        for key, rec in sorted(videos.items()):
            files = vid_local.get(key, [])
            if not files:
                row = dict(rec); row['local_files'] = ''; missing.append(row)
    return missing, img_local, vid_local


def ensure_log_dir(path): path.mkdir(parents=True, exist_ok=True)

def write_json(path, data):
    with path.open('w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2)

def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore'); w.writeheader()
        for row in rows:
            clean = dict(row)
            for k, v in list(clean.items()):
                if isinstance(v, list): clean[k] = ';'.join(map(str, v))
            w.writerow(clean)


def image_ok(content, content_type):
    if not content or content.lstrip().lower().startswith((b'<!doctype html', b'<html')): return False
    if content.startswith(b'\xff\xd8\xff') or content.startswith(b'\x89PNG\r\n\x1a\n') or content.startswith((b'GIF87a', b'GIF89a')): return True
    if content.startswith(b'RIFF') and content[8:12] == b'WEBP': return True
    return content_type.lower().startswith('image/') and len(content) > 500


def video_ok(content, content_type):
    if not content or content.lstrip().lower().startswith((b'<!doctype html', b'<html')): return False
    # MP4/QuickTime files normally have an ftyp box near the beginning.
    if len(content) >= 12 and content[4:8] == b'ftyp': return True
    return content_type.lower().startswith('video/') and len(content) > 100_000


def download_one(rec, target_dir, kind, timeout=None):
    key = rec['key']
    existing = local_image_files(target_dir).get(key, []) if kind == 'image' else local_video_files(target_dir).get(key, [])
    if existing:
        return {'kind': kind, 'key': key, 'status': 'already_exists', 'file': ';'.join(p.name for p in existing), 'url_used': '', 'error': ''}
    candidates = image_candidates(rec['url'], rec.get('snapshot_ts', '')) if kind == 'image' else video_candidates(rec['url'], rec.get('snapshot_ts', ''))
    errors = []
    headers = {'User-Agent': USER_AGENT, 'Referer': 'https://web.archive.org/'}
    timeout = timeout or ((8, 60) if kind == 'video' else (8, 40))
    for url in candidates:
        tmp = None
        try:
            with requests.get(url, headers=headers, timeout=timeout, stream=True) as resp:
                resp.raise_for_status()
                ctype = resp.headers.get('Content-Type', '')
                # Stream to temporary file so large videos do not need to sit in RAM.
                tmp = target_dir / f'.{kind}_{key}.repair.tmp'
                total = 0
                head = bytearray()
                with tmp.open('wb') as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if not chunk: continue
                        if len(head) < 64:
                            head.extend(chunk[:64-len(head)])
                        f.write(chunk); total += len(chunk)
                    f.flush(); os.fsync(f.fileno())
                if kind == 'image':
                    content = bytes(head)
                    if not image_ok(content, ctype):
                        raise ValueError(f'响应不是有效图片 (Content-Type={ctype}, bytes={total})')
                    if content.startswith(b'\x89PNG\r\n\x1a\n'): ext = '.png'
                    elif content.startswith((b'GIF87a', b'GIF89a')): ext = '.gif'
                    elif content.startswith(b'RIFF') and content[8:12] == b'WEBP': ext = '.webp'
                    elif content.startswith(b'\xff\xd8\xff'): ext = '.jpg'
                    else: ext = ext_from_url(url)
                    target = target_dir / safe_media_filename(rec['url'], rec.get('snapshot_ts', ''), 'image')
                    # Preserve the extension implied by actual image bytes when it differs.
                    base = target.stem
                    target = target_dir / (base + ext)
                else:
                    if not video_ok(bytes(head), ctype):
                        raise ValueError(f'响应不是可识别的视频 (Content-Type={ctype}, bytes={total})')
                    target = target_dir / safe_media_filename(rec['url'], rec.get('snapshot_ts', ''), 'video')
                if total <= 0:
                    raise ValueError('下载文件为空')
                os.replace(tmp, target)
                return {'kind': kind, 'key': key, 'status': 'downloaded', 'file': target.name, 'url_used': url, 'error': '', 'bytes': total}
        except Exception as e:
            if tmp and tmp.exists():
                try: tmp.unlink()
                except OSError: pass
            errors.append(f'{url} -> {type(e).__name__}: {e}')
    return {'kind': kind, 'key': key, 'status': 'failed', 'file': '', 'url_used': '', 'error': ' || '.join(errors)}


def run_scan(which):
    root, json_dir, image_dir, video_dir, log_dir = locate_paths()
    ensure_log_dir(log_dir); image_dir.mkdir(parents=True, exist_ok=True); video_dir.mkdir(parents=True, exist_ok=True)
    images, videos, json_files, malformed = collect_media(json_dir)
    missing, img_local, vid_local = get_missing(images, videos, image_dir, video_dir, which)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report = {'created_at': datetime.now().isoformat(timespec='seconds'), 'account_dir': str(Path.cwd()),
              'json_files_scanned': len(json_files), 'unique_images': len(images), 'unique_videos': len(videos),
              'local_image_keys': len(img_local), 'local_video_keys': len(vid_local),
              'missing_count': len(missing), 'missing': missing, 'malformed_json': malformed,
              'note': '独立扫描；不读取或修改 archive.py 的任何状态文件。'}
    jp = log_dir / f'scan_{which}_{stamp}.json'; cp = log_dir / f'missing_{which}_{stamp}.csv'
    write_json(jp, report)
    write_csv(cp, missing, ['kind','key','url','snapshot_ts','source_json','source_jsons','local_files'])
    print(f'扫描 JSON：{len(json_files)} 份')
    print(f'唯一图片资源：{len(images)}；本地缺失：{sum(1 for k in images if not local_image_files(image_dir).get(k))}')
    print(f'唯一视频资源：{len(videos)}；本地缺失：{sum(1 for k in videos if not local_video_files(video_dir).get(k))}')
    print(f'本次扫描类型：{which}；缺失项目：{len(missing)}；无法读取 JSON：{len(malformed)}')
    print(f'独立报告：{jp}\n缺失清单：{cp}')
    return 0


def run_download(which):
    root, json_dir, image_dir, video_dir, log_dir = locate_paths()
    ensure_log_dir(log_dir); image_dir.mkdir(parents=True, exist_ok=True); video_dir.mkdir(parents=True, exist_ok=True)
    images, videos, json_files, malformed = collect_media(json_dir)
    missing, _, _ = get_missing(images, videos, image_dir, video_dir, which)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    plan_path = log_dir / f'download_plan_{which}_{stamp}.json'
    write_json(plan_path, {'created_at': datetime.now().isoformat(timespec='seconds'), 'json_files_scanned': len(json_files),
                           'missing_before_download': len(missing), 'items': missing, 'malformed_json': malformed,
                           'note': '只补本地缺失的图片/视频；不改原工具状态文件。'})
    print(f'扫描到唯一图片 {len(images)} 个、唯一视频 {len(videos)} 个；本次缺失待补 {len(missing)} 项。')
    print(f'独立计划日志：{plan_path}')
    results = []
    for i, rec in enumerate(missing, 1):
        kind = rec['kind']; folder = image_dir if kind == 'image' else video_dir
        print(f'[{i}/{len(missing)}] {kind} {rec["key"]} ({rec.get("source_json", "")}) ...', flush=True)
        result = download_one(rec, folder, kind)
        result.update({'url': rec['url'], 'snapshot_ts': rec.get('snapshot_ts', ''), 'source_json': rec.get('source_json', '')})
        results.append(result)
        if result['status'] == 'downloaded': print(f'  成功：{result["file"]} ({result.get("bytes", 0)} bytes)')
        elif result['status'] == 'already_exists': print(f'  已存在：{result["file"]}')
        else: print('  失败：所有候选地址均未成功；原因已记入日志。')
        time.sleep(0.2 if kind == 'image' else 0.5)
    jp = log_dir / f'download_results_{which}_{stamp}.json'; cp = log_dir / f'download_results_{which}_{stamp}.csv'
    summary = {'created_at': datetime.now().isoformat(timespec='seconds'), 'total': len(results),
               'downloaded': sum(r['status'] == 'downloaded' for r in results),
               'already_exists': sum(r['status'] == 'already_exists' for r in results),
               'failed': sum(r['status'] == 'failed' for r in results), 'results': results,
               'malformed_json': malformed, 'note': '独立日志；未修改 archive.py 状态文件。'}
    write_json(jp, summary)
    write_csv(cp, results, ['kind','key','status','file','url','url_used','bytes','error','snapshot_ts','source_json'])
    print('\n修复结果：')
    print(f'  新下载：{summary["downloaded"]}；已存在：{summary["already_exists"]}；失败：{summary["failed"]}')
    print(f'结果日志：{jp}\n结果 CSV：{cp}')
    return 0 if summary['failed'] == 0 else 1


def main():
    parser = argparse.ArgumentParser(description='独立图片/视频缺失扫描与修复工具')
    sub = parser.add_subparsers(dest='command', required=True)
    for cmd in ('scan', 'download'):
        p = sub.add_parser(cmd, help='扫描并报告' if cmd == 'scan' else '重新扫描后尝试下载所有缺失媒体')
        p.add_argument('--type', choices=('all','images','videos'), default='all', help='处理类型，默认 all')
    args = parser.parse_args()
    try:
        return run_scan(args.type) if args.command == 'scan' else run_download(args.type)
    except KeyboardInterrupt:
        print('\n用户中断；已完成的文件保留，下次运行会按本地文件重新检查。', file=sys.stderr)
        return 130
    except Exception as e:
        print(f'错误：{type(e).__name__}: {e}', file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
'''

# ===== status_cleaner.py: original implementation (embedded) =====
_SOURCE_STATUS = r'''
#!/usr/bin/env python3
"""Reconcile IncandescenceArchiver status records against files actually on disk.

Only changes the `status` field of existing records in _log/archive_index.json,
then regenerates the corresponding status .txt views. It never downloads,
renames, deletes, or edits HTML/JSON/media/profile files.

Usage (from accounts/<username>):
  python ..\\..\\status_cleaner.py scan
  python ..\\..\\media_repair.py status
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
    report_dir = root / "media_repair_logs"
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
        print("\n这是只读预览，没有修改任何文件。确认结果后运行：python ..\\..\\media_repair.py status")
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
'''

# ===== html_media_repair.py: original implementation (embedded) =====
_SOURCE_HTML = r'''
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
'''


def _load_original(name: str):
    # Execute each original source inside an isolated module namespace.
    # The original __name__ guard stays inactive; no external .py required.
    source = globals()['_' + 'SOURCE_' + name.upper()]
    module = types.ModuleType('_media_repair_' + name)
    module.__file__ = str(Path(__file__).resolve())
    module.__dict__['__builtins__'] = __builtins__
    exec(compile(source, str(Path(__file__).resolve()) + ' [' + name + ']', 'exec'), module.__dict__)
    return module


class _Tee(io.TextIOBase):
    def __init__(self, *outputs):
        self.outputs = outputs

    def write(self, s):
        for out in self.outputs:
            out.write(s)
            out.flush()
        return len(s)

    def flush(self):
        for out in self.outputs:
            out.flush()


def _root_for_reports() -> Path:
    cwd = Path.cwd().resolve()
    if (cwd / 'wayback_snapshots').is_dir():
        return cwd / 'wayback_snapshots'
    if (cwd / 'json').is_dir() and (cwd / 'html').is_dir():
        return cwd
    accounts = cwd / 'accounts'
    if accounts.is_dir():
        candidates = [p / 'wayback_snapshots' for p in accounts.iterdir()
                      if (p / 'wayback_snapshots' / 'json').is_dir()]
        if len(candidates) == 1:
            return candidates[0]
    raise RuntimeError('找不到 wayback_snapshots 归档目录；请从 accounts\用户名 目录运行。')


def _perform(name: str, scan: bool, media_type: str = 'all', root_arg: str | None = None) -> int:
    print(f'\n{"=" * 20} {name} / {"scan" if scan else "download/apply"} {"=" * 20}', flush=True)
    module = _load_original(name)
    if name == 'avatar':
        return int(module.scan(None) if scan else module.download(None))
    if name == 'imgvid':
        return int(module.run_scan(media_type) if scan else module.run_download(media_type))
    if name == 'status':
        return int(module.run(apply=not scan))
    if name == 'html':
        old_argv = sys.argv
        try:
            sys.argv = [old_argv[0], 'scan' if scan else 'apply']
            if root_arg:
                sys.argv.extend(['--root', root_arg])
            return int(module.main())
        finally:
            sys.argv = old_argv
    raise ValueError(f'未知子指令: {name}')


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description='头像、图片视频、状态、HTML 四合一修复：无参数依次执行全部 download/apply。',
        epilog='扫描必须指定子指令，例如 media_repair.py avatar --scan；无子指令不能使用 --scan。',
    )
    sub = parser.add_subparsers(dest='command')
    for command, help_text in (
        ('avatar', '头像缺失补下载'),
        ('imgvid', '图片/视频缺失补下载'),
        ('status', '归档状态修正及 TXT 清单更新'),
        ('html', '补回 HTML 缺失的本地图片标签'),
    ):
        p = sub.add_parser(command, help=help_text)
        p.add_argument('--scan', action='store_true', help='只执行该项原有 scan，不执行 download/apply')
        if command == 'imgvid':
            p.add_argument('--type', choices=('all', 'images', 'videos'), default='all',
                           help='处理类型，默认 all')
        if command == 'html':
            p.add_argument('--root', help='沿用原 HTML 修复脚本的 --root 参数')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    commands = (args.command,) if args.command else ORDER
    scan = bool(getattr(args, 'scan', False))
    media_type = getattr(args, 'type', 'all')
    root_arg = getattr(args, 'root', None)
    try:
        if root_arg and args.command == 'html':
            # An explicit HTML root may point at a different archive.
            candidate = Path(root_arg).expanduser().resolve()
            if (candidate / 'wayback_snapshots').is_dir():
                candidate = candidate / 'wayback_snapshots'
            report_root = candidate
        else:
            report_root = _root_for_reports()
        report_dir = report_root / 'media_repair_logs'
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        label = args.command or 'runall'
        report_file = report_dir / f'media_repair_{label}_{"scan" if scan else "run"}_{stamp}.log'
    except (OSError, RuntimeError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 2

    results: list[tuple[str, int]] = []
    interrupted = False
    with report_file.open('w', encoding='utf-8') as log:
        with contextlib.redirect_stdout(_Tee(sys.stdout, log)), contextlib.redirect_stderr(_Tee(sys.stderr, log)):
            print(f'归档：{report_root}')
            print(f'报告目录：{report_dir}')
            print(f'执行顺序：{" -> ".join(commands)}')
            for name in commands:
                try:
                    code = _perform(name, scan=scan, media_type=media_type, root_arg=root_arg)
                except KeyboardInterrupt:
                    interrupted = True
                    code = 130
                    print(f'中断：{name}；已完成的修改保留，后续步骤未执行。', file=sys.stderr)
                except BaseException as exc:
                    code = 2
                    print(f'[错误] {name}: {type(exc).__name__}: {exc}', file=sys.stderr)
                    traceback.print_exc()
                results.append((name, code))
                print(f'[{name}] 结束，退出码={code}')
                if interrupted:
                    break
            print('\n========== 总结 ==========')
            for name, code in results:
                print(f'{name}: {"成功" if code == 0 else f"失败/未完全成功（退出码 {code}）"}')
            print(f'完整运行日志：{report_file}')
    return 130 if interrupted else (1 if any(code != 0 for _, code in results) else 0)


if __name__ == '__main__':
    raise SystemExit(main())
