"""Inline the vendored hub pages for our nonce CSP; --check never writes."""
import argparse
import base64
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / 'server/src/agentmesh_server/vendor/openhuddle/src/hub/pages'
OUTPUTS = [ROOT / 'server/src/agentmesh_server', ROOT / 'cloudflare/src']
NONCE = '__AGENTMESH_NONCE__'


def source(name):
    return ''.join(line for line in (PAGES / name).read_text().splitlines(keepends=True)
                   if 'Provenance: OpenHuddle ' not in line)


def build(name):
    page = source(name + '.html')
    page = re.sub(r'<link rel="stylesheet" href="/static/([\w.-]+)">',
                  lambda m: f'<style nonce="{NONCE}">\n{source(m[1])}</style>', page)
    page = re.sub(r'<script src="/static/([\w.-]+)"([^>]*)></script>',
                  lambda m: f'<script nonce="{NONCE}"{m[2]}>\n{source(m[1])}</script>', page)
    page = re.sub(r'/static/([\w.-]+\.svg)', lambda m: 'data:image/svg+xml;base64,' +
                  base64.b64encode(source(m[1]).encode()).decode(), page)
    if '/static/' in page:
        raise ValueError(f'Uninlined asset in {name}')
    return (f'<!-- Provenance: OpenHuddle 59105e5 src/hub/pages/{name}.html, MIT; '
            'adapted: generated nonce-CSP inlined assets -->\n' + page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    stale = []
    for name in ['dashboard', 'join']:
        content = build(name)
        for directory in OUTPUTS:
            target = directory / f'hub_{name}.html'
            if args.check:
                if not target.exists() or target.read_text() != content:
                    stale.append(str(target.relative_to(ROOT)))
            else:
                target.write_text(content)
    if stale:
        parser.exit(1, 'Stale hub pages: ' + ', '.join(stale) + '\n')
    print('Hub pages up to date' if args.check else 'Hub pages generated')


if __name__ == '__main__':
    main()
