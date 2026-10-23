"""Check OpenHuddle provenance, upstream hashes and third-party inventory offline."""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDORS = [
    ROOT / 'server/src/agentmesh_server/vendor/openhuddle',
    ROOT / 'client/src/agentmesh_mcp/vendor/openhuddle',
    ROOT / 'cloudflare/src/openhuddle',
]
UPSTREAM = Path('/tmp/oh-scratch-team/src')
MARKER = 'Provenance: OpenHuddle 59105e5 '


def original_text(path):
    return ''.join(line for line in path.read_text().splitlines(keepends=True) if MARKER not in line)


def check():
    failures = []
    notices = (ROOT / 'docs/THIRD_PARTY.md').read_text()
    count = 0
    for vendor in VENDORS:
        entries = {}
        for line in (vendor / 'UPSTREAM.sha256').read_text().splitlines():
            digest, name = line.split('  ', 1)
            if name in entries or not re.fullmatch('[a-f0-9]{64}', digest):
                failures.append(f'Invalid/duplicate upstream manifest entry: {name}')
            if not (vendor / name).resolve().is_relative_to(vendor.resolve()):
                failures.append(f'Manifest path outside vendor: {name}')
                continue
            entries[name] = digest
        candidates = {vendor / name for name in entries}
        # Include additional ports (e.g. db-do.js) not present at their upstream path.
        candidates.update(p for p in vendor.rglob('*') if p.is_file() and
                          p.suffix in {'.js', '.ts', '.html', '.css', '.svg', '.md', '.json'}
                          and MARKER in p.read_text())
        for path in sorted(candidates):
            relative = str(path.relative_to(ROOT))
            name = str(path.relative_to(vendor))
            if not path.exists():
                failures.append(f'Missing vendored file: {relative}')
                continue
            lines = path.read_text().splitlines()
            provenance = [line for line in lines if MARKER in line]
            if len(provenance) != 1:
                failures.append(f'Expected exactly one provenance line: {relative}')
                continue
            line = provenance[0]
            match = re.search(r'Provenance: OpenHuddle 59105e5 ([^,]+), MIT; (verbatim|adapted: .+)', line)
            if not match:
                failures.append(f'Invalid provenance: {relative}')
                continue
            source, mode = match.groups()
            if relative not in notices:
                failures.append(f'Missing THIRD_PARTY entry: {relative}')
            if name in entries and mode.startswith('verbatim'):
                actual = hashlib.sha256(original_text(path).encode()).hexdigest()
                if actual != entries[name]:
                    failures.append(f'Verbatim upstream hash mismatch: {relative}')
            if UPSTREAM.is_dir() and name in entries:
                origin = UPSTREAM / source
                if not origin.resolve().is_relative_to(UPSTREAM.resolve()) or not origin.is_file():
                    failures.append(f'Unknown upstream source: {relative}')
                elif hashlib.sha256(origin.read_bytes()).hexdigest() != entries[name]:
                    failures.append(f'Manifest does not match upstream snapshot: {relative}')
            count += 1
    # Dashboard behavior must be byte-faithful; only its storage namespace changes.
    pages = VENDORS[0] / 'src/hub/pages'
    dashboard = original_text(pages / 'dashboard.js').replace('agentmesh:', 'openhuddle:')
    expected = (UPSTREAM / 'src/hub/pages/dashboard.js')
    if expected.is_file() and dashboard != expected.read_text():
        failures.append('Dashboard JavaScript differs beyond the storage namespace')
    if 'innerHTML' in dashboard:
        failures.append('Dashboard must render teammate data using textContent, not innerHTML')
    for name in ['base.css', 'dashboard.css']:
        origin = UPSTREAM / 'src/hub/pages' / name
        if origin.is_file():
            # Theme changes may replace CSS variable declarations, not layout or controls.
            without_vars = lambda s: re.sub(r'^\s*--[\w-]+:.*$', '', s, flags=re.MULTILINE)
            if without_vars(original_text(pages / name)) != without_vars(origin.read_text()):
                failures.append(f'CSS differs beyond palette/font variables: {name}')
    server_seed = VENDORS[0] / 'agentmesh-seed.js'
    worker_seed = VENDORS[2] / 'agentmesh-seed.js'
    if server_seed.read_bytes() != worker_seed.read_bytes():
        failures.append('Python/Worker seed adapters must be byte-identical')
    if failures:
        raise SystemExit('\n'.join(failures))
    print(f'Vendor provenance, hashes and notices verified ({count} files)')


if __name__ == '__main__':
    check()
