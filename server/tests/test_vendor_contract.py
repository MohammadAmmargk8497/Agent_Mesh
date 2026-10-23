"""The Worker boundary must not fork OpenHuddle's domain or schema semantics."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / 'server/src/agentmesh_server/vendor/openhuddle'
WORKER = ROOT / 'cloudflare/src/openhuddle'


def unmarked(path):
    return ''.join(line for line in path.read_text().splitlines(keepends=True)
                   if 'Provenance: OpenHuddle ' not in line)


def test_worker_retains_upstream_schema_and_migration_steps():
    original = unmarked(SERVER / 'src/hub/db.js')
    port = unmarked(WORKER / 'src/hub/db-do.js')
    for name in ('SCHEMA', 'HUDDLE_TABLES'):
        pattern = rf'const {name} = `(.+?)`;'
        assert re.search(pattern, port, re.DOTALL)[1] == re.search(pattern, original, re.DOTALL)[1]
    pattern = r'function huddles\(db\) \{(.+?)\n\}'
    assert re.search(pattern, port, re.DOTALL)[1] == re.search(pattern, original, re.DOTALL)[1]
    assert 'const MIGRATIONS = [null, SCHEMA, huddles];' in port


def test_worker_domain_only_changes_transaction_and_backup_boundaries():
    def normalized(path):
        source = unmarked(path)
        source = re.sub(r'^import .+ from \'node:(?:fs|path)\';\n', '', source, flags=re.MULTILINE)
        source = re.sub(r'\n  tx\(fn\) \{.+?(?=\n  flush\(\))', '\n', source, flags=re.DOTALL)
        source = re.sub(r'\n  backup\(dir\) \{.+?(?=\n  // ---- delegated domain)', '\n', source, flags=re.DOTALL)
        return source

    assert normalized(WORKER / 'src/hub/hub.js') == normalized(SERVER / 'src/hub/hub.js')


def test_client_cli_only_adapts_bootstrap_and_keeps_upstream_error_reporting():
    original = unmarked(SERVER / 'bin/openhuddle.js')
    expected = original.replace("from '../src/client/cli.js'", "from '../src/client/agentmesh-boundary.js'")
    expected = expected.replace('main(process.argv.slice(2))', "main('cli', process.argv.slice(2))")
    client_bin = ROOT / 'client/src/agentmesh_mcp/vendor/openhuddle/bin/openhuddle.js'
    assert unmarked(client_bin) == expected


def test_vendor_and_generated_artifacts_are_current():
    for script, arguments in (('check_vendor.py', []), ('build_hub_pages.py', ['--check'])):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *arguments],
                                cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
