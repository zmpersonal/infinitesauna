#!/usr/bin/env python3
"""Generate static permanent-equivalent redirect pages for GitHub Pages.

GitHub Pages cannot emit route-specific HTTP 301 responses. Google documents an
instant meta refresh as a permanent redirect signal, so each legacy path gets a
dedicated page with an instant refresh, canonical target and JS fallback.
"""
import argparse
import csv
import html
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'data' / 'legacy_redirects.csv'
TARGET = 'https://infinitesauna.com/'


def safe_destination(source):
    path = urlsplit(source).path
    decoded = unquote(path).strip('/')
    parts = PurePosixPath(decoded).parts
    if not decoded or any(part in ('', '.', '..') for part in parts):
        return None
    destination = ROOT.joinpath(*parts)
    if destination.suffix:
        destination = destination.parent / destination.name
    return destination / 'index.html'


def redirect_html(source):
    escaped_source = html.escape(source, quote=True)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="0; url={TARGET}"><link rel="canonical" href="{TARGET}"><title>Moved permanently | Infinite Sauna</title><script>location.replace({TARGET!r});</script></head><body><p>This legacy page has moved to <a href="{TARGET}" rel="nofollow">Infinite Sauna</a>.</p><small>Former address: {escaped_source}</small></body></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Validate without writing redirect pages')
    args = parser.parse_args()
    created = skipped = invalid = 0
    seen = set()
    destinations = set()
    with MANIFEST.open(newline='', encoding='utf-8-sig') as handle:
        for row in csv.DictReader(handle):
            source = (row.get('source') or '').strip()
            if not source or source in seen:
                continue
            seen.add(source)
            destination = safe_destination(source)
            if destination is None:
                invalid += 1
                continue
            if destination in destinations:
                skipped += 1
                continue
            destinations.add(destination)
            if destination.exists():
                skipped += 1
                continue
            if not args.check:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(redirect_html(source), encoding='utf-8')
            created += 1
    action = 'would create' if args.check else 'created'
    print(f'{action} {created} legacy redirect pages; skipped {skipped} live routes; rejected {invalid} unsafe routes.')


if __name__ == '__main__':
    main()
