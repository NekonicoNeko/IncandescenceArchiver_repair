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
    """Match archive.py safe_filename(): strip query, sanitize URL, keep first 100 chars."""
    url_no_query = (url or '').split('?', 1)[0]
    clean = re.sub(r"[^\w\-_]", "_", url_no_query.replace("https://", "").replace("http://", ""))
    if kind == 'image':
        path = urlparse(url_no_query).path.lower()
        ext = next((e for e in ('.png', '.gif', '.webp', '.jpeg', '.jpg') if path.endswith(e)), '.jpg')
    else:
        ext = '.mp4'
    return f'{timestamp or "00000000000000"}_{clean[:100]}{ext}'


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
                    # Use archive.py's URL-based extension and exact filename convention.
                    target = target_dir / safe_media_filename(rec['url'], rec.get('snapshot_ts', ''), 'image')
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
