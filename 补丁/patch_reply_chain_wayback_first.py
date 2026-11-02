#!/usr/bin/env python3
"""Patch NekonicoNeko's Windows-fixed download_archive.py: local -> Wayback -> X API.

Usage (Windows):
  python patch_reply_chain_wayback_first.py
  python patch_reply_chain_wayback_first.py "path\\to\\webarchived_tweet_downloader修复版download_archive.py"
Creates download_archive.py next to the source, without overwriting existing files.
"""
import argparse
import ast
from pathlib import Path

SOURCE_FILENAME = 'webarchived_tweet_downloader修复版download_archive.py'
START = 'def fetch_reply_chain_payload(\n'
END = '\ndef recover_reply_chain_payloads_by_conversation('

EXPECTED = '''def fetch_reply_chain_payload(
    tweet_id: str,
    auth_value,
    lookup_context: Optional[ReplyChainLookupContext],
    author_hint: str = "",
    access_index: Optional[dict] = None,
) -> Tuple[Optional[dict], str, str]:
    context = lookup_context or ReplyChainLookupContext()
    effective_author_hint = author_hint or context.author_hint
    if context.use_local_json:
        payload = find_local_snapshot_payload_for_tweet(tweet_id, context.json_dir, context.snapshot_timestamp)
        if payload is not None:
            return payload, "", "local_json"
    x_api_error = ""
    if normalize_x_api_auths(auth_value):
        payload, x_api_error, source = fetch_x_api_tweet_payload_with_auths(
            auth_value,
            tweet_id,
            access_index=access_index,
            author_hint=effective_author_hint,
        )
        if payload is not None:
            return payload, "", source
    wayback_error = ""
    if context.use_wayback:
        payload, wayback_error, source = fetch_wayback_tweet_payload(
            tweet_id,
            effective_author_hint,
            context.snapshot_timestamp,
        )
        if payload is not None:
            return payload, "", source
    if x_api_error and wayback_error:
        return None, f"{x_api_error} Wayback fallback: {wayback_error}", ""
    return None, x_api_error or wayback_error or "Reply-chain lookup did not find this post.", ""
'''

REPLACEMENT = '''def fetch_reply_chain_payload(
    tweet_id: str,
    auth_value,
    lookup_context: Optional[ReplyChainLookupContext],
    author_hint: str = "",
    access_index: Optional[dict] = None,
) -> Tuple[Optional[dict], str, str]:
    context = lookup_context or ReplyChainLookupContext()
    effective_author_hint = author_hint or context.author_hint
    if context.use_local_json:
        payload = find_local_snapshot_payload_for_tweet(tweet_id, context.json_dir, context.snapshot_timestamp)
        if payload is not None:
            return payload, "", "local_json"
    wayback_error = ""
    if context.use_wayback:
        payload, wayback_error, source = fetch_wayback_tweet_payload(
            tweet_id,
            effective_author_hint,
            context.snapshot_timestamp,
        )
        if payload is not None:
            return payload, "", source
    x_api_error = ""
    if normalize_x_api_auths(auth_value):
        payload, x_api_error, source = fetch_x_api_tweet_payload_with_auths(
            auth_value,
            tweet_id,
            access_index=access_index,
            author_hint=effective_author_hint,
        )
        if payload is not None:
            return payload, "", source
    if wayback_error and x_api_error:
        return None, f"{wayback_error} X API fallback: {x_api_error}", ""
    return None, wayback_error or x_api_error or "Reply-chain lookup did not find this post.", ""
'''


def main():
    p = argparse.ArgumentParser(description='Put free Wayback before paid X API for parent-reply lookup.')
    p.add_argument('source', nargs='?', type=Path, default=Path(__file__).resolve().parent / SOURCE_FILENAME)
    p.add_argument('--output', type=Path, help='Output path (default: download_archive.py next to source)')
    args = p.parse_args()
    source = args.source.resolve()
    if not source.is_file():
        p.error(f'Source file not found: {source}')
    target = (args.output or source.with_name('download_archive.py')).resolve()
    if target == source:
        p.error('Refusing to overwrite the original source')
    if target.exists():
        p.error(f'Output already exists, refusing to overwrite: {target}')
    raw = source.read_bytes()
    if raw.startswith(b'\xef\xbb\xbf'):
        p.error('Unexpected UTF-8 BOM; source was not modified')
    try:
        data = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        p.error(f'Source is not UTF-8: {exc}')
    newline = '\r\n' if b'\r\n' in raw else '\n'
    normalized = data.replace('\r\n', '\n')
    if normalized.count(START) != 1 or normalized.count(END) != 1:
        p.error('Expected reply-chain function boundary not uniquely found; no changes made')
    start = normalized.index(START)
    end = normalized.index(END, start)
    actual = normalized[start:end].rstrip('\n') + '\n'
    if actual != EXPECTED:
        p.error('Source function differs from the verified GitHub patch version; no changes made')
    modified = normalized[:start] + REPLACEMENT + normalized[end:]
    try:
        ast.parse(modified, filename=str(target))
        compile(modified, str(target), 'exec')
    except SyntaxError as exc:
        p.error(f'Python syntax check failed: {exc}; no changes made')
    # Verify the only change is the one inspected function.
    assert modified.replace(REPLACEMENT, EXPECTED, 1) == normalized
    with target.open('xb') as out:
        out.write(modified.replace('\n', newline).encode('utf-8'))
    print(f'OK: wrote {target}')
    print('Lookup order: local JSON -> Wayback -> X API')
    print('Original source preserved. No API calls performed.')


if __name__ == '__main__':
    main()
