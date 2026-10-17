#!/usr/bin/env python3
"""独立修复 IncandescenceArchiver 缺失的视频 preview image。

命令：scan / download / convert <webarchived_tweet_downloader dump_dir>
只向 wayback_snapshots/image/ 添加预览图，并写独立日志；不修改 archive.py 状态、HTML 或 JSON。
"""
from __future__ import annotations
import argparse, csv, json, os, re, shutil, sys, time
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
TIMESTAMP_RE = re.compile(r'^(\d{14})_')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
PROXY_BASE = os.environ.get('TWITTER_PROXY_BASE', '').rstrip('/')


def locate_paths():
    cwd = Path.cwd()
    root = cwd / 'wayback_snapshots'
    if (root / 'json').is_dir() and (root / 'image').is_dir():
        return root, root / 'json', root / 'image', root / 'preview_image_repair_logs'
    accounts = cwd / 'accounts'
    if accounts.is_dir():
        candidates = [a / 'wayback_snapshots' for a in accounts.iterdir()
                      if a.is_dir() and (a / 'wayback_snapshots' / 'json').is_dir()]
        if len(candidates) == 1:
            root = candidates[0]
            return root, root / 'json', root / 'image', root / 'preview_image_repair_logs'
    raise RuntimeError('找不到 wayback_snapshots/json。请在 accounts\\用户名 目录运行。')


def timestamp_from_name(name):
    m = TIMESTAMP_RE.match(name)
    return m.group(1) if m else ''


def preview_urls_from_json(json_dir: Path):
    by_url, malformed = {}, []
    files = sorted(json_dir.glob('*.json'))
    for jp in files:
        try:
            with jp.open('r', encoding='utf-8-sig') as f:
                data = json.load(f)
        except Exception as e:
            malformed.append({'json': jp.name, 'error': f'{type(e).__name__}: {e}'})
            continue
        if not isinstance(data, dict):
            continue
        ts = timestamp_from_name(jp.name)
        for media in ((data.get('includes') or {}).get('media') or []):
            if not isinstance(media, dict) or media.get('type') not in ('video', 'animated_gif'):
                continue
            url = str(media.get('preview_image_url') or '').strip()
            if not url:
                continue
            rec = by_url.get(url)
            if rec is None:
                by_url[url] = {'url': url, 'snapshot_ts': ts, 'source_json': jp.name,
                               'source_jsons': [jp.name], 'media_key': str(media.get('media_key') or ''),
                               'media_type': str(media.get('type') or '')}
            else:
                rec['source_jsons'].append(jp.name)
                if ts and (not rec['snapshot_ts'] or ts < rec['snapshot_ts']):
                    rec['snapshot_ts'], rec['source_json'] = ts, jp.name
                if not rec['media_key'] and media.get('media_key'):
                    rec['media_key'] = str(media['media_key'])
    for rec in by_url.values():
        rec['source_jsons'] = sorted(set(rec['source_jsons']))
    return by_url, files, malformed


def url_ext(url):
    # 与 archive.py 的 ext_from_url() 一致：只看去掉 query 后的路径；
    # 不根据 ?format=... 猜扩展名，未知时默认 .jpg。
    path = url.split('?', 1)[0].lower()
    for ext in ('.png', '.gif', '.webp', '.jpeg', '.jpg'):
        if path.endswith(ext):
            return ext
    return '.jpg'


def safe_local_name(url, ts, ext=None):
    # 与 archive.py safe_filename(timestamp, url, ext) 完全一致。
    # URL 去 query、去协议，清洗后最多取 100 字符；扩展名由调用方按原工具规则传入。
    ts = ts or '00000000000000'
    ext = ext or url_ext(url)
    url_no_query = url.split('?', 1)[0]
    clean = re.sub(r'[^\w\-_]', '_', url_no_query.replace('https://', '').replace('http://', ''))
    return f'{ts}_{clean[:100]}{ext}'


def target_for(rec, image_dir, ext=None):
    return image_dir / safe_local_name(rec['url'], rec.get('snapshot_ts', ''), ext)


def existing_target(rec, image_dir):
    # convert 使用 downloader 源文件扩展名，可能与 URL 推断扩展名不同；
    # scan 因此检查同一规范化文件名主体的常见图片扩展名，避免误报缺失。
    expected = target_for(rec, image_dir)
    if expected.is_file() and expected.stat().st_size > 0:
        return expected
    stem = expected.stem
    for ext in IMAGE_EXTS:
        candidate = image_dir / f'{stem.rsplit(".", 1)[0]}{ext}'
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    # 源扩展名变化时，目标名主体仍是同一 safe_filename 清洗结果；
    # 用 timestamp + URL-clean 前缀匹配其余扩展名。
    prefix = expected.name.rsplit('.', 1)[0]
    for ext in IMAGE_EXTS:
        candidate = image_dir / f'{prefix}{ext}'
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def ensure_dirs(log_dir, image_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)


def write_json(path, data):
    with path.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for row in rows:
            clean = dict(row)
            for k, v in list(clean.items()):
                if isinstance(v, list): clean[k] = ';'.join(map(str, v))
            w.writerow(clean)


def collect_missing(records, image_dir):
    missing = []
    for rec in records.values():
        dst = target_for(rec, image_dir)
        found = existing_target(rec, image_dir)
        row = dict(rec)
        row['expected_file'] = dst.name
        row['existing_file'] = found.name if found else ''
        row['local_exists'] = found is not None
        if not row['local_exists']: missing.append(row)
    return sorted(missing, key=lambda r: (r.get('snapshot_ts', ''), r['url']))


def scan(args):
    root, json_dir, image_dir, log_dir = locate_paths()
    ensure_dirs(log_dir, image_dir)
    records, files, malformed = preview_urls_from_json(json_dir)
    missing = collect_missing(records, image_dir)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report = {'created_at': datetime.now().isoformat(timespec='seconds'), 'archive_root': str(root),
              'json_files_scanned': len(files), 'unique_preview_urls': len(records),
              'existing_preview_files': len(records)-len(missing), 'missing_preview_files': len(missing),
              'malformed_json_count': len(malformed), 'missing': missing, 'malformed_json': malformed,
              'note': '只读扫描；未修改 JSON/HTML/archive.py 状态文件。'}
    jp, cp = log_dir / f'scan_{stamp}.json', log_dir / f'missing_{stamp}.csv'
    write_json(jp, report)
    write_csv(cp, missing, ['media_key','media_type','url','snapshot_ts','source_json','source_jsons','expected_file','existing_file','local_exists'])
    print(f'扫描 JSON：{len(files)} 份')
    print(f'唯一视频预览图 URL：{len(records)}')
    print(f'本地目标文件存在：{len(records)-len(missing)}')
    print(f'本地目标文件缺失：{len(missing)}')
    print(f'无法读取的 JSON：{len(malformed)}')
    print(f'扫描报告：{jp}\n缺失清单：{cp}')
    return 0


def proxy_url(url):
    p = urlparse(url)
    if PROXY_BASE and p.netloc in ('pbs.twimg.com','video.twimg.com','abs.twimg.com'):
        return f'{PROXY_BASE}/{p.netloc}{p.path}' + (f'?{p.query}' if p.query else '')
    return url


def candidate_urls(rec):
    original, ts = rec['url'], rec.get('snapshot_ts', '')
    out = []
    def add(u):
        if u and u not in out: out.append(u)
    add(proxy_url(original))
    p = urlparse(original)
    query = dict(parse_qsl(p.query, keep_blank_values=True))
    if p.netloc.endswith('pbs.twimg.com') and p.path.startswith('/media/') and query.get('format'):
        fmt = query['format']
        add(proxy_url(urlunparse(p._replace(path=p.path if Path(p.path).suffix else p.path+'.'+fmt, query=''))))
        for name in ('orig','4096x4096','large','medium','small','900x900'):
            q = dict(query); q.update(format=fmt, name=name)
            add(proxy_url(urlunparse(p._replace(query=urlencode(q)))))
    if ts:
        for mode in ('im_', 'if_', ''):
            add(f'https://web.archive.org/web/{ts}{mode}/{original}')
    return out


def valid_image(head, ctype, total):
    if not head or head.lstrip().lower().startswith((b'<!doctype html', b'<html')): return False
    if (head.startswith(b'\xff\xd8\xff') or head.startswith(b'\x89PNG\r\n\x1a\n') or
        head.startswith((b'GIF87a', b'GIF89a')) or (head.startswith(b'RIFF') and head[8:12] == b'WEBP')):
        return True
    return ctype.lower().startswith('image/') and total > 500


def signature_ext(head, url):
    if head.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if head.startswith((b'GIF87a', b'GIF89a')): return '.gif'
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP': return '.webp'
    if head.startswith(b'\xff\xd8\xff'): return '.jpg'
    return url_ext(url)


def download_one(rec, image_dir):
    dst = target_for(rec, image_dir)
    found = existing_target(rec, image_dir)
    if found is not None:
        return {'url': rec['url'], 'status': 'already_exists', 'file': found.name, 'url_used': '', 'bytes': found.stat().st_size, 'error': ''}
    errors, headers = [], {'User-Agent': USER_AGENT, 'Referer': 'https://web.archive.org/'}
    for url in candidate_urls(rec):
        tmp = image_dir / f'.{dst.stem}.preview_repair.tmp'
        try:
            with requests.get(url, headers=headers, timeout=(8,40), stream=True) as resp:
                resp.raise_for_status()
                ctype, total, head = resp.headers.get('Content-Type',''), 0, bytearray()
                with tmp.open('wb') as f:
                    for chunk in resp.iter_content(chunk_size=128*1024):
                        if not chunk: continue
                        if len(head) < 64: head.extend(chunk[:64-len(head)])
                        f.write(chunk); total += len(chunk)
                    f.flush(); os.fsync(f.fileno())
                if not valid_image(bytes(head), ctype, total):
                    raise ValueError(f'响应不是有效图片 (Content-Type={ctype}, bytes={total})')
                # Keep the URL-derived extension so scan/convert use the same deterministic filename.
                final = dst
                os.replace(tmp, final)
                return {'url': rec['url'], 'status': 'downloaded', 'file': final.name, 'url_used': url, 'bytes': total, 'error': ''}
        except Exception as e:
            try: tmp.unlink(missing_ok=True)
            except OSError: pass
            errors.append(f'{url} -> {type(e).__name__}: {e}')
    return {'url': rec['url'], 'status': 'failed', 'file': '', 'url_used': '', 'bytes': 0, 'error': ' || '.join(errors)}


def download(args):
    root, json_dir, image_dir, log_dir = locate_paths()
    ensure_dirs(log_dir, image_dir)
    records, _, malformed = preview_urls_from_json(json_dir)
    missing = collect_missing(records, image_dir)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    plan = log_dir / f'download_plan_{stamp}.json'
    write_json(plan, {'created_at': datetime.now().isoformat(timespec='seconds'), 'missing_before_download': len(missing), 'items': missing})
    print(f'扫描到 {len(records)} 个唯一预览图 URL；缺失 {len(missing)} 个。\n下载计划：{plan}')
    if not missing:
        print('没有缺失的预览图。'); return 0
    results = []
    for i, rec in enumerate(missing, 1):
        print(f'[{i}/{len(missing)}] {rec["url"]}', flush=True)
        r = download_one(rec, image_dir)
        r.update({'media_key':rec.get('media_key',''), 'snapshot_ts':rec.get('snapshot_ts',''), 'source_json':rec.get('source_json','')})
        results.append(r)
        print(f'  {r["status"]}: {r.get("file") or r.get("error","")[:220]}')
        time.sleep(.15)
    jp, cp = log_dir / f'download_results_{stamp}.json', log_dir / f'download_results_{stamp}.csv'
    summary = {'created_at':datetime.now().isoformat(timespec='seconds'), 'attempted':len(results),
               'downloaded':sum(r['status']=='downloaded' for r in results),
               'already_exists':sum(r['status']=='already_exists' for r in results),
               'failed':sum(r['status']=='failed' for r in results), 'results':results, 'malformed_json':malformed}
    write_json(jp, summary)
    write_csv(cp, results, ['media_key','url','status','file','url_used','bytes','error','snapshot_ts','source_json'])
    print(f'\n新下载：{summary["downloaded"]}；已存在：{summary["already_exists"]}；失败：{summary["failed"]}\n结果日志：{jp}\n结果 CSV：{cp}')
    return 0 if summary['failed']==0 else 1


def find_dump_assets(dump_dir):
    direct = dump_dir / 'snapshots.json'
    if direct.is_file(): return direct, dump_dir
    for child in dump_dir.iterdir():
        if child.is_dir() and (child/'snapshots.json').is_file(): return child/'snapshots.json', child
    for base, dirs, files in os.walk(dump_dir):
        if 'snapshots.json' in files: return Path(base)/'snapshots.json', Path(base)
        if Path(base) != dump_dir and Path(base).parent != dump_dir: dirs[:] = []
    return None, None


def resolve_dump_file(dump_dir, assets_dir, rel):
    relp = Path(rel)
    for p in (dump_dir/relp, assets_dir/relp, assets_dir.parent/relp, assets_dir/'media'/relp.name):
        if p.is_file(): return p
    return None


def convert(args):
    root, json_dir, image_dir, log_dir = locate_paths()
    ensure_dirs(log_dir, image_dir)
    dump_dir = Path(args.dump_dir).expanduser().resolve()
    if not dump_dir.is_dir():
        print(f'错误：dump 目录不存在：{dump_dir}', file=sys.stderr); return 2
    snapshots_path, assets_dir = find_dump_assets(dump_dir)
    if not snapshots_path:
        print(f'错误：在 {dump_dir} 中找不到 snapshots.json', file=sys.stderr); return 2
    index_path = assets_dir/'media_index.json'
    if not index_path.is_file():
        print(f'错误：找不到 {index_path}', file=sys.stderr); return 2
    try:
        with index_path.open('r', encoding='utf-8-sig') as f: media_index = json.load(f)
    except Exception as e:
        print(f'错误：media_index.json 读取失败：{e}', file=sys.stderr); return 2
    if not isinstance(media_index, dict):
        print('错误：media_index.json 不是 URL → 文件路径映射。', file=sys.stderr); return 2
    records, _, malformed = preview_urls_from_json(json_dir)
    # 仅导入当前归档 JSON 实际引用的 preview_image_url，不导入其他孤立缩略图。
    results, copied, exists, source_missing = [], 0, 0, 0
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    for url, rec in records.items():
        rel = media_index.get(url)
        if not rel:
            continue
        src = resolve_dump_file(dump_dir, assets_dir, str(rel))
        if not src:
            source_missing += 1
            results.append({'url':url,'status':'source_missing','file':'','source':str(rel),'error':'media_index 指向的文件不存在'})
            continue
        # 与 archive.py cmd_convert 一致：优先使用 media_index 相对路径的扩展名；
        # 源路径无扩展名时才使用 archive.py 的 ext_from_url() 规则。
        ext_in_hash = Path(str(rel)).suffix.lower()
        ext = ext_in_hash if ext_in_hash else url_ext(url)
        final = target_for(rec, image_dir, ext)
        if final.is_file() and final.stat().st_size > 0:
            exists += 1
            results.append({'url':url,'status':'already_exists','file':final.name,'source':'','error':''})
            continue
        tmp = image_dir/f'.{final.name}.convert.tmp'
        try:
            with src.open('rb') as rf, tmp.open('wb') as wf:
                shutil.copyfileobj(rf,wf); wf.flush(); os.fsync(wf.fileno())
            if not tmp.stat().st_size: raise ValueError('源文件为空')
            os.replace(tmp,final); copied += 1
            results.append({'url':url,'status':'copied','file':final.name,'source':str(src),'error':''})
        except Exception as e:
            try: tmp.unlink(missing_ok=True)
            except OSError: pass
            results.append({'url':url,'status':'failed','file':'','source':str(src),'error':f'{type(e).__name__}: {e}'})
    jp, cp = log_dir/f'convert_results_{stamp}.json', log_dir/f'convert_results_{stamp}.csv'
    write_json(jp, {'created_at':datetime.now().isoformat(timespec='seconds'),'dump_dir':str(dump_dir),
                    'media_index':str(index_path),'referenced_preview_urls':len(records),'copied':copied,
                    'already_exists':exists,'source_missing':source_missing,'malformed_json_count':len(malformed),
                    'results':results,'note':'只复制 JSON 中引用且 media_index 有记录的 preview_image_url；不修改 HTML/JSON/archive.py 状态文件。'})
    write_csv(cp,results,['url','status','file','source','error'])
    print(f'本地 JSON 引用的唯一 preview_image_url：{len(records)}')
    print(f'从 downloader 复制：{copied}\n目标文件已存在：{exists}\nmedia_index 有记录但源文件缺失：{source_missing}')
    print(f'结果日志：{jp}\n结果 CSV：{cp}')
    return 0 if source_missing==0 and not any(r['status']=='failed' for r in results) else 1


def main():
    parser = argparse.ArgumentParser(description='IncandescenceArchiver 视频预览图独立修复工具')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('scan', help='扫描 JSON 引用的视频预览图，并检查 image/ 是否缺失')
    sub.add_parser('download', help='从 preview_image_url / Wayback 下载缺失预览图')
    cp = sub.add_parser('convert', help='从 webarchived_tweet_downloader 的 media_index.json 复制预览图')
    cp.add_argument('dump_dir', help='上游归档目录（含 snapshots.json 或 *_archive_assets）')
    args = parser.parse_args()
    try:
        if args.command=='scan': return scan(args)
        if args.command=='download': return download(args)
        return convert(args)
    except KeyboardInterrupt:
        print('\n用户中断；已完成文件保留，下次运行会重新检查。', file=sys.stderr); return 130
    except Exception as e:
        print(f'错误：{type(e).__name__}: {e}', file=sys.stderr); return 2

if __name__ == '__main__':
    raise SystemExit(main())
