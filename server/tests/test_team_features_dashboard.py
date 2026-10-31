"""Execute the composed home dashboard against a disposable local home server."""

import json
import re
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

from agentmesh_server.config import init_config
from agentmesh_server.dashboard_features import compose_dashboard
from agentmesh_server.hub import find_node
from test_hub_seed import running_hub


ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("team_features_dashboard.cjs")


@pytest.fixture
def feature_dashboard_api(tmp_path):
    directory = tmp_path / "feature-dashboard"
    config = init_config(directory)
    with running_hub(directory, config) as api:
        yield api


@pytest.mark.parametrize("alter", ["missing", "duplicate"])
def test_composer_rejects_changed_or_ambiguous_insertion_points(alter):
    template = files("agentmesh_server").joinpath("dashboard.html").read_text(encoding="utf-8")
    anchor = "        function takeToken() {"
    changed = template.replace(anchor, "" if alter == "missing" else anchor + anchor)
    with pytest.raises(ValueError, match="home dashboard extension anchor changed"):
        compose_dashboard(changed)


def test_both_home_routes_compose_features_without_embedded_credentials(feature_dashboard_api):
    api = feature_dashboard_api
    template = files("agentmesh_server").joinpath("dashboard.html").read_text(encoding="utf-8")
    for route in ("/", "/t/testteam"):
        response = api.get(route, None)
        assert response.status == 200
        nonce = re.search(r"script-src 'nonce-([^']+)'", response.headers["content-security-policy"])[1]
        page = response.body.decode()
        assert page == compose_dashboard(template).replace("__AGENTMESH_NONCE__", nonce)
        assert f'<script nonce="{nonce}">' in page
        assert f'<style nonce="{nonce}">' in page
        assert 'id="team-notes-panel"' in page
        assert "renderTaskHandoffs(t)" in page
        assert all(token not in page for token in api.tokens.values())
        assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert api.server.hub.process is None, "composing a page must not start the hub"


def test_home_features_render_and_write_through_real_member_routes(feature_dashboard_api):
    api = feature_dashboard_api
    result = subprocess.run(
        [find_node(), str(HARNESS)], cwd=ROOT,
        input=json.dumps({"origin": f"http://{api.host}:{api.port}", "legacy_token": api.tokens["a"]}),
        text=True, capture_output=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "team-features-dashboard-ok" in result.stdout
