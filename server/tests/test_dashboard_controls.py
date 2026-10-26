"""Run dashboard controls against the real authenticated Python HTTP routes."""

import json
import re
import subprocess

from agentmesh_server.config import init_config
from test_dashboard import BROWSER_SMOKE, dashboard_document
from test_hub_seed import running_hub

# Keep the existing browser-safety harness authoritative. This adapter adds the
# small pieces of native form behavior needed by write controls and forwards
# fetches to the fixture's HTTP server instead of pretending a route exists.
_BROWSER = BROWSER_SMOKE.split("const flush = ")[0]
_BROWSER = _BROWSER.replace("nextStatus = 200", "nextStatus = null")
_BROWSER = _BROWSER.replace(
    "this.value = ''; this.className = ''; this.scrollTop = 0;",
    "this.value = ''; this.className = ''; this.scrollTop = 0; "
    "this.checked = false; this.disabled = false; this.type = ''; this.name = '';",
)
_BROWSER = _BROWSER.replace(
    "if (name === 'class') this.className = String(value);",
    "if (name === 'class') this.className = String(value); "
    "if (['value', 'type', 'name'].includes(name)) this[name] = String(value); "
    "if (['checked', 'disabled'].includes(name)) this[name] = true;",
)
_BROWSER = _BROWSER.replace(
    "if (name === 'class') return this.className || null;",
    "if (name === 'class') return this.className || null; "
    "if (['checked', 'disabled'].includes(name)) return this[name] ? '' : null;",
)
_BROWSER = _BROWSER.replace(
    "click() { this.fire('click'); }",
    "click() { if (!this.disabled) this.fire('click'); }\n"
    "    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }\n"
    "    reset() { for (const field of this.querySelectorAll('input, textarea, select')) "
    "{ field.value = field.getAttribute('value') || ''; field.checked = false; } }",
)
_FETCH_START = _BROWSER.index("    fetch: async (url, options) => {")
_FETCH_END = _BROWSER.index("    },\n  });", _FETCH_START)
_BROWSER = _BROWSER[:_FETCH_START] + r"""
    fetch: async (url, options = {}) => {
      const call = {url, options, done: false}; calls.push(call);
      const status = nextStatus; nextStatus = null;
      const pending = deferred; deferred = null;
      let response;
      if (status !== null) {
        call.status = status;
        response = {status, ok: status >= 200 && status < 300,
          json: async () => ({error: 'The server rejected this action; refresh and try again.'})};
      } else {
        const actual = await fetch(input.origin + url, options);
        const payload = await actual.json();
        call.status = actual.status; call.payload = payload;
        response = {status: actual.status, ok: actual.ok, json: async () => payload};
      }
      if (pending) await pending;
      call.done = true;
      return response;
""" + _BROWSER[_FETCH_END:]


_BROWSER += r"""
const flush = () => new Promise(resolve => setTimeout(resolve, 10));
async function waitFor(predicate, message) {
  const end = Date.now() + 8000;
  while (Date.now() < end) {
    if (predicate()) return;
    await flush();
  }
  throw new Error('Timed out: ' + message);
}
async function idle(app) {
  await waitFor(() => app.calls.every(call => call.done), 'HTTP requests complete');
  await flush(); await flush();
  assertKnownRoutes(app);
}
function assertKnownRoutes(app) {
  const base = new Set(['/v1/huddles', '/v1/master/sections', '/v1/master/submit', '/v1/master/vote', '/v1/questions', '/v1/claims', '/v1/claims/release', '/v1/settings']);
  const uuid = '[0-9a-f-]{36}';
  const patterns = [new RegExp('^/v1/dashboard/tasks/' + uuid + '/(take|accept|decline|start|release|finish)$'),
    new RegExp('^/v1/tasks/' + uuid + '/review$'), new RegExp('^/v1/huddles/' + uuid + '/(posts|comments|votes|close)$'),
    new RegExp('^/v1/questions/' + uuid + '/answer$')];
  const actions = new Set(['task-take', 'task-accept', 'task-decline', 'task-start', 'task-release', 'task-finish',
    'task-review-accept', 'task-review-reopen', 'master-edit', 'master-save', 'master-cancel', 'master-create',
    'master-submit', 'master-approve', 'master-reject', 'huddle-start', 'huddle-idea', 'huddle-comment',
    'huddle-vote-up', 'huddle-vote-down', 'huddle-close', 'question-ask', 'question-answer', 'claim-file', 'claim-release', 'settings-auto-respond']);
  for (const control of app.root.querySelectorAll('[data-route]')) {
    assert.ok(base.has(control.dataset.route) || patterns.some(pattern => pattern.test(control.dataset.route)),
      'every rendered control targets a known HTTP route: ' + control.dataset.route);
  }
  for (const control of app.root.querySelectorAll('[data-action]')) {
    assert.ok(actions.has(control.dataset.action), 'every rendered action is covered by these control scenarios: ' + control.dataset.action);
    assert.ok(control.dataset.route, 'every action declares its route');
  }
  for (const button of app.root.querySelectorAll('button')) if (button.dataset.write === 'true') {
    assert.ok(button.dataset.action && button.dataset.route, 'every mutation button declares its tested action and route');
  }
}
async function login(app, role) {
  assert.equal(app.get('access-mode'), null, 'one access form auto-detects writable credentials');
  app.get('access-key').value = input.tokens[role];
  app.get('access-form').fire('submit');
  await idle(app);
  assert.match(app.get('connection-status').textContent, /Live/);
}
async function poll(app) {
  assert.equal(app.timers.size, 1, 'exactly one refresh timer remains active');
  await [...app.timers.values()][0]();
  await idle(app);
}
function set(app, id, value) {
  const control = app.get(id);
  assert.ok(control, 'control exists: ' + id);
  control.value = value;
  control.fire('input'); control.fire('change');
  return control;
}
function action(app, name, route = null) {
  const candidates = app.root.querySelectorAll('button').filter(node =>
    node.dataset.action === name && (!route || node.dataset.route === route));
  assert.equal(candidates.length, 1, 'one action button for ' + name + ' ' + (route || ''));
  return candidates[0];
}
async function submit(app, id) {
  const form = app.get(id);
  assert.ok(form, 'form exists: ' + id);
  const count = app.calls.length;
  form.fire('submit'); await idle(app);
  const posts = app.calls.slice(count).filter(call => call.options.method === 'POST');
  assert.equal(posts.length, 1, 'one write per submit: ' + id);
  assert.ok(posts[0].status >= 200 && posts[0].status < 300,
    id + ' calls a successful existing route: ' + posts[0].url + ' (' + posts[0].status + ')');
  return posts[0];
}
async function click(app, name, route) {
  const button = action(app, name, route), count = app.calls.length;
  assert.equal(Boolean(button.disabled), false, name + ' action is enabled');
  button.click(); await idle(app);
  const posts = app.calls.slice(count).filter(call => call.options.method === 'POST');
  assert.equal(posts.length, 1, 'one write per click: ' + name);
  assert.ok(posts[0].status >= 200 && posts[0].status < 300,
    name + ' calls a successful existing route: ' + posts[0].url + ' (' + posts[0].status + ')');
  return posts[0];
}
async function api(path, body, role = 'a') {
  const response = await fetch(input.origin + path, {
    method: 'POST', headers: {'Authorization': 'Bearer ' + input.tokens[role], 'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await response.json();
  assert.ok(response.ok, 'test setup route: ' + path + ' ' + response.status);
  return data;
}
"""


def _run_browser(api, scenario, seed=None):
    html, document = dashboard_document()
    payload = {
        "scripts": re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.DOTALL | re.IGNORECASE),
        "tree": document.tree,
        "origin": f"http://{api.host}:{api.port}",
        "tokens": api.tokens,
        "seed": seed or {},
    }
    result = subprocess.run(
        ["node", "-e", _BROWSER + "\n(async () => {\n" + scenario + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"],
        input=json.dumps(payload), text=True, capture_output=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _seed_controls(api):
    """Independent stateful targets keep every UI action meaningful."""
    seed = {"tasks": {}}
    for action in ("take", "accept", "decline", "start", "release", "finish"):
        response = api.post("/v1/tasks", {
            "title": "Dashboard " + action,
            "description": "Exercise the " + action + " control",
            "assignee": "agent-b" if action == "take" else "agent-a",
            "acceptance_criteria": ["Works through the dashboard"],
        }, "a" if action == "take" else "b")
        assert response.status == 201
        task_id = response.json()["id"]
        seed["tasks"][action] = task_id
        if action in ("start", "release", "finish"):
            assert api.post(f"/v1/dashboard/tasks/{task_id}/accept", {}).status == 200
        if action == "finish":
            assert api.post(f"/v1/dashboard/tasks/{task_id}/start", {}).status == 200
    for decision in ("accept", "reopen"):
        task_id = api.task()
        assert api.post(f"/v1/tasks/{task_id}/claim", {}, "b").status == 200
        assert api.post(f"/v1/tasks/{task_id}/complete", {"summary": "Ready to review"}, "b").status == 200
        seed["tasks"]["review_" + decision] = task_id
    response = api.post("/v1/huddles", {"kind": "brainstorm", "topic": "Dashboard huddle"})
    assert response.status == 201
    seed["huddle"] = response.json()["id"]
    response = api.post(f"/v1/huddles/{seed['huddle']}/posts", {"kind": "idea", "text": "Another agent's idea"}, "b")
    assert response.status == 201
    seed["idea"] = response.json()["id"]
    response = api.post("/v1/questions", {"to": "agent-a", "text": "Can you answer through the dashboard?"}, "b")
    assert response.status == 201
    seed["question"] = response.json()["id"]
    assert api.post("/v1/claims", {"path": "src/dashboard-release.txt"}).status == 201
    assert api.post("/v1/master/sections", {"section": "Goal", "text": "Original goal"}).status == 201
    return seed


def test_dashboard_task_and_prompt_controls_call_real_routes(api):
    seed = _seed_controls(api)
    _run_browser(api, r"""
  const app = browser(), get = app.get;
  assert.equal(app.calls.length, 0, 'no fetch before login');
  await login(app, 'a');
  assert.match(app.calls[0].url, /^\/v1\/dashboard\/state\?after=0$/);
  assert.equal(app.storage.has('agentmesh-viewer'), false, 'agent tokens stay memory-only');
  assert.ok(![...app.storage.values()].includes(input.tokens.a), 'agent token is not persisted under another key');
  for (const name of ['take', 'accept', 'decline', 'start', 'release']) {
    const path = '/v1/dashboard/tasks/' + input.seed.tasks[name] + '/' + name;
    const call = await click(app, 'task-' + name, path);
    assert.equal(call.url, path);
    assert.deepEqual(JSON.parse(call.options.body), {});
  }
  const finishId = input.seed.tasks.finish;
  set(app, 'task-finish-summary-' + finishId, 'Finished from a real dashboard control');
  const finish = await submit(app, 'task-finish-form-' + finishId);
  assert.equal(finish.url, '/v1/dashboard/tasks/' + finishId + '/finish');
  assert.equal(JSON.parse(finish.options.body).summary, 'Finished from a real dashboard control');
  for (const decision of ['accept', 'reopen']) {
    const path = '/v1/tasks/' + input.seed.tasks['review_' + decision] + '/review';
    const call = await click(app, 'task-review-' + decision, path);
    assert.deepEqual(JSON.parse(call.options.body), {decision});
  }

  action(app, 'master-edit').click();
  set(app, 'master-section-text-Goal', 'Goal edited through the dashboard');
  const beforeEdit = app.calls.length;
  await api('/v1/master/sections', {section: 'Goal', text: 'Remote edit during a local draft'}, 'b');
  await poll(app);
  assert.equal(get('master-section-text-Goal').value, 'Goal edited through the dashboard', 'polling preserves editor drafts');
  assert.equal(app.calls.slice(beforeEdit).filter(call => call.options.method === 'POST').length, 0);
  const saved = await submit(app, 'master-edit-Goal');
  assert.equal(saved.url, '/v1/master/sections');
  assert.deepEqual(JSON.parse(saved.options.body), {section: 'Goal', text: 'Goal edited through the dashboard'});
  action(app, 'master-edit').click();
  set(app, 'master-section-text-Goal', 'Unsaved cancellation');
  const beforeCancel = app.calls.length;
  action(app, 'master-cancel').click();
  assert.equal(get('master-edit-Goal'), null, 'cancel closes the section editor');
  assert.equal(app.calls.length, beforeCancel, 'cancel is a local action');
  set(app, 'master-section-name', 'Constraints');
  set(app, 'master-section-new-text', 'Preserve both dashboard hosts');
  const created = await submit(app, 'master-new-section-form');
  assert.equal(created.url, '/v1/master/sections');
  assert.deepEqual(JSON.parse(created.options.body), {section: 'Constraints', text: 'Preserve both dashboard hosts'});
  await click(app, 'master-submit', '/v1/master/submit');
  assert.match(get('prompt-badge').textContent, /voting/);
  const other = browser(); await login(other, 'b');
  set(other, 'master-vote-note', 'Reviewed the new section and approve');
  const approval = await click(other, 'master-approve', '/v1/master/vote');
  assert.deepEqual(JSON.parse(approval.options.body), {decision: 'approve', note: 'Reviewed the new section and approve'});
  assert.match(other.get('prompt-badge').textContent, /locked/);
  assert.match(other.get('news').textContent, /agent-b approved master prompt/);
  assert.match(other.get('news').textContent, /Reviewed the new section and approve/);

  await poll(app);
  const goalEdit = get('master').querySelectorAll('button').find(node =>
    node.dataset.action === 'master-edit' && node.closest('article').querySelector('h3').textContent === 'Goal');
  assert.ok(goalEdit); goalEdit.click();
  set(app, 'master-section-text-Goal', 'A second revision needs a second vote');
  await submit(app, 'master-edit-Goal');
  await click(app, 'master-submit', '/v1/master/submit');
  await poll(other);
  set(other, 'master-vote-note', 'Please clarify the second revision');
  const rejection = await click(other, 'master-reject', '/v1/master/vote');
  assert.deepEqual(JSON.parse(rejection.options.body), {decision: 'reject', note: 'Please clarify the second revision'});
  assert.match(other.get('prompt-badge').textContent, /draft/);
  assert.match(other.get('news').textContent, /agent-b rejected master prompt/);
  assert.match(other.get('news').textContent, /Please clarify the second revision/);
  assert.match(other.get('news').textContent, /Reviewed the new section and approve/, 'feed retains historical vote comments');
  for (const [client, role] of [[app, 'a'], [other, 'b']]) for (const call of client.calls) {
    assert.ok(call.options.headers.Authorization === 'Bearer ' + input.tokens[role], 'request carries the session bearer token');
    assert.equal(call.options.cache, 'no-store');
    if (call.options.method === 'POST') assert.equal(call.options.headers['Content-Type'], 'application/json');
  }
""", seed)


def test_dashboard_rejected_viewer_and_invalid_credentials_never_offer_writes(tmp_path):
    directory = tmp_path / "dashboard-auth"
    config = init_config(directory)
    with running_hub(directory, config) as api:
        _seed_controls(api)
        _run_browser(api, r"""
  const app = browser();
  assert.equal(app.get('access-mode'), null, 'viewer mode is not offered');
  app.get('access-key').value = 'short';
  app.get('access-form').fire('submit');
  assert.equal(app.calls.length, 0, 'short credentials are rejected without a request');
  assert.equal(app.get('auth-error').hidden, false);
  for (const token of [input.tokens.viewer, 'x'.repeat(40)]) {
    app.get('access-key').value = token;
    app.get('access-form').fire('submit');
    await idle(app);
    assert.equal(app.get('auth').hidden, false, 'removed viewer and invalid credentials stay at authentication');
    assert.equal(app.get('workspace').hidden, true);
    assert.equal(app.timers.size, 0);
    assert.equal(app.root.querySelectorAll('[data-write]').length, 0, 'rejected credentials never expose write controls');
    assert.equal(app.get('messages').textContent, '');
    assert.equal(app.get('member-heartbeats').textContent, '');
    assert.equal(app.storage.has('agentmesh-viewer'), false);
  }
  assert.ok(app.calls.some(call => call.url.startsWith('/v1/dashboard/state') && call.status === 401), 'viewer token is rejected by agent authentication');
  assert.ok(!app.calls.some(call => call.url.startsWith('/v1/transcript')), 'removed viewer transcript is never requested');
  assert.ok(app.calls.every(call => (call.options.method || 'GET') === 'GET'), 'credential detection cannot mutate state');
        """)


def test_dashboard_huddles_questions_claims_settings_and_commit_events(api):
    seed = _seed_controls(api)
    unsafe = '<img src=x onerror="globalThis.injected=true"><script>bad()</script>'
    assert api.post("/v1/commits", {"sha": "abcdef123", "subject": unsafe, "branch": "dashboard", "files": ["src/dashboard.txt"]}, "b").status == 201
    seed["unsafe"] = unsafe
    _run_browser(api, r"""
  const app = browser(), get = app.get;
  await login(app, 'a');
  const startKinds = ['brainstorm', 'design', 'review', 'standup'];
  for (const kind of startKinds) {
    set(app, 'huddle-kind', kind);
    set(app, 'huddle-topic', 'Started a ' + kind + ' through the dashboard');
    const call = await submit(app, 'huddle-start-form');
    assert.equal(call.url, '/v1/huddles');
    assert.deepEqual(JSON.parse(call.options.body), {kind, topic: 'Started a ' + kind + ' through the dashboard'});
    assert.equal(get('huddle-topic').value, '', 'a successful start clears its topic draft');
    const started = get('huddle-list').querySelectorAll('button').find(node => node.dataset.focus === 'huddle:' + call.payload.id);
    assert.ok(started);
    assert.match(started.closest('article').textContent, kind === 'standup' ? /Waiting on: agent-a, agent-b/ : /Waiting on: agent-b/);
  }
  const huddleId = input.seed.huddle, ideaId = input.seed.idea;
  const open = get('huddle-list').querySelectorAll('button').find(node => node.dataset.focus === 'huddle:' + huddleId);
  assert.ok(open); open.click();
  assert.equal(get('huddle-view').hidden, false);
  assert.match(get('huddle-view').textContent, /Everyone has weighed in/, 'starter and posting peer have both contributed');
  set(app, 'huddle-idea-text-' + huddleId, input.seed.unsafe);
  const idea = await submit(app, 'huddle-idea-form-' + huddleId);
  assert.equal(idea.url, '/v1/huddles/' + huddleId + '/posts');
  assert.deepEqual(JSON.parse(idea.options.body), {kind: 'idea', text: input.seed.unsafe});
  set(app, 'huddle-comment-text-' + huddleId, 'A discussion comment on that idea');
  set(app, 'huddle-comment-ref-' + huddleId, ideaId);
  const comment = await submit(app, 'huddle-comment-form-' + huddleId);
  assert.equal(comment.url, '/v1/huddles/' + huddleId + '/comments');
  assert.deepEqual(JSON.parse(comment.options.body), {text: 'A discussion comment on that idea', ref_post_id: ideaId});
  for (const vote of ['up', 'down']) {
    set(app, 'huddle-vote-note-' + ideaId, 'The ' + vote + ' vote has a comment');
    const call = await click(app, 'huddle-vote-' + vote, '/v1/huddles/' + huddleId + '/votes');
    assert.deepEqual(JSON.parse(call.options.body), {ref_post_id: ideaId, vote, note: 'The ' + vote + ' vote has a comment'});
  }
  const otherIdea = get('huddle-view').querySelectorAll('.idea').find(node => node.textContent.includes("Another agent's idea"));
  assert.equal(otherIdea.querySelector('.backing').textContent, 'Back it +0', 'new vote replaces the previous vote');
  assert.equal(otherIdea.querySelector('.pushback').textContent, 'Push back −1');
  assert.match(get('huddle-view').textContent, /The down vote has a comment/);
  assert.match(get('huddle-view').textContent, /Everyone has weighed in/);
  assert.equal(get('huddle-view').querySelectorAll('img').length, 0, 'idea text does not create HTML');
  set(app, 'huddle-decision-' + huddleId, 'Proceed with the reviewed proposal');
  const closed = await submit(app, 'huddle-close-form-' + huddleId);
  assert.equal(closed.url, '/v1/huddles/' + huddleId + '/close');
  assert.equal(JSON.parse(closed.options.body).decision, 'Proceed with the reviewed proposal');
  assert.equal(get('huddle-close-form-' + huddleId), null, 'closed huddle has no owner write form');
  assert.match(get('huddle-view').textContent, /Proceed with the reviewed proposal/);
  get('huddle-back').click();

  set(app, 'question-text', input.seed.unsafe);
  set(app, 'question-task', input.seed.tasks.take);
  const asked = await submit(app, 'question-ask-form');
  assert.equal(asked.url, '/v1/questions');
  assert.deepEqual(JSON.parse(asked.options.body), {to: 'agent-b', text: input.seed.unsafe, task_id: input.seed.tasks.take});
  set(app, 'question-answer-text-' + input.seed.question, 'Answered through the dashboard');
  const answered = await submit(app, 'question-answer-form-' + input.seed.question);
  assert.equal(answered.url, '/v1/questions/' + input.seed.question + '/answer');
  assert.deepEqual(JSON.parse(answered.options.body), {text: 'Answered through the dashboard'});
  assert.equal(get('question-answer-form-' + input.seed.question), null, 'answered question cannot be answered twice');
  assert.ok(get('questions').textContent.includes(input.seed.unsafe));
  assert.equal(get('questions').querySelectorAll('img').length, 0);

  const release = await click(app, 'claim-release', '/v1/claims/release');
  assert.deepEqual(JSON.parse(release.options.body), {path: 'src/dashboard-release.txt'});
  set(app, 'claim-path', 'src/dashboard-created.txt');
  set(app, 'claim-task', input.seed.tasks.take);
  const claimed = await submit(app, 'claim-file-form');
  assert.equal(claimed.url, '/v1/claims');
  assert.deepEqual(JSON.parse(claimed.options.body), {path: 'src/dashboard-created.txt', task_id: input.seed.tasks.take});
  assert.match(get('claims').textContent, /src\/dashboard-created.txt/);
  for (const enabled of [false, true]) {
    const toggle = get('auto-respond'), count = app.calls.length;
    toggle.checked = enabled; toggle.fire('change'); await idle(app);
    const writes = app.calls.slice(count).filter(call => call.options.method === 'POST');
    assert.equal(writes.length, 1);
    assert.equal(writes[0].url, '/v1/settings');
    assert.equal(writes[0].status, 200);
    assert.deepEqual(JSON.parse(writes[0].options.body), {auto_respond: enabled});
    assert.equal(get('auto-respond').checked, enabled);
  }
  assert.ok(get('news').textContent.includes(input.seed.unsafe), 'commit event includes the announced subject');
  assert.match(get('news').textContent, /abcdef123/);
  assert.equal(get('news').querySelectorAll('img').length, 0, 'commit text is rendered literally');
  for (const kind of ['huddle', 'question', 'file', 'master']) assert.match(get('news').textContent.toLowerCase(), new RegExp(kind));
  for (const call of app.calls) {
    assert.ok(call.options.headers.Authorization === 'Bearer ' + input.tokens.a, 'every control uses the agent bearer credential');
    if (call.options.method === 'POST') assert.equal(call.options.headers['Content-Type'], 'application/json');
  }
""", seed)


def test_dashboard_write_errors_drafts_duplicate_submit_and_logout_races(api):
    seed = _seed_controls(api)
    _run_browser(api, r"""
  const app = browser(), get = app.get;
  await login(app, 'a');
  const beforeEmpty = app.calls.length;
  get('question-ask-form').fire('submit');
  await idle(app);
  assert.equal(app.calls.length, beforeEmpty, 'empty question fails locally without a write');
  assert.match(get('action-status').textContent, /Question is required/);

  set(app, 'claim-path', '../outside.txt');
  const beforeInvalid = app.calls.length;
  get('claim-file-form').fire('submit'); await idle(app);
  const invalid = app.calls.slice(beforeInvalid).filter(call => call.options.method === 'POST');
  assert.equal(invalid.length, 1);
  assert.equal(invalid[0].status, 400, 'server rejects invalid paths even when JS is bypassed');
  assert.equal(get('claim-path').value, '../outside.txt', 'validation errors retain the draft');
  assert.equal(get('workspace').hidden, false, 'validation errors do not end an authenticated session');
  assert.match(get('action-status').className, /error/);
  assert.equal(get('claim-path').disabled, false, 'failed writes re-enable controls');

  set(app, 'claim-path', 'src/permission-retry.txt');
  app.setStatus(403);
  get('claim-file-form').fire('submit'); await idle(app);
  assert.equal(get('workspace').hidden, false, 'a permission rejection is not a credential expiry');
  assert.equal(get('claim-path').value, 'src/permission-retry.txt');
  assert.match(get('action-status').textContent, /server rejected this action/);
  assert.equal(get('claim-path').disabled, false);

  set(app, 'question-text', 'Keep my draft while another agent asks a question');
  set(app, 'question-task', input.seed.tasks.take);
  get('question-text').focus();
  await api('/v1/questions', {to: 'agent-a', text: 'A new question refreshes the panel'}, 'b');
  await poll(app);
  assert.equal(get('question-text').value, 'Keep my draft while another agent asks a question');
  assert.equal(get('question-task').value, input.seed.tasks.take);
  assert.equal(app.document.activeElement, get('question-text'), 'refresh restores focused draft input');

  set(app, 'question-text', 'Submit exactly once while awaiting the server');
  const release = app.deferNext(), beforeRepeat = app.calls.length;
  get('question-ask-form').fire('submit');
  get('question-ask-form').fire('submit');
  await waitFor(() => app.calls[beforeRepeat]?.status === 201, 'first question reaches real server');
  assert.equal(app.calls.length - beforeRepeat, 1, 'repeat submit cannot send another in-flight mutation');
  assert.ok(app.root.querySelectorAll('[data-write]').every(node => node.disabled), 'all write controls lock while saving');
  release(); await idle(app);
  assert.equal(get('question-text').value, '', 'successful question clears the draft');
  assert.equal(get('question-task').value, '', 'successful question clears optional task');
  assert.equal(get('question-text').disabled, false);

  set(app, 'question-text', 'An expired session must clear this private draft');
  app.setStatus(401);
  get('question-ask-form').fire('submit'); await idle(app);
  assert.equal(get('auth').hidden, false);
  assert.equal(get('workspace').hidden, true);
  assert.equal(app.timers.size, 0);
  assert.match(get('auth-error').textContent, /agent token was rejected/);
  assert.equal(get('questions').textContent, '', 'expired credentials clear private transcript text');
  await login(app, 'a');
  assert.equal(get('question-text').value, '', 'a new session does not restore an old private draft');

  set(app, 'claim-path', 'src/late-dashboard-write.txt');
  const releaseLate = app.deferNext(), beforeLate = app.calls.length;
  get('claim-file-form').fire('submit');
  await waitFor(() => app.calls[beforeLate]?.status === 201, 'late write reaches real server');
  get('sign-out').click();
  assert.equal(app.calls[beforeLate].options.signal.aborted, true, 'logout aborts the in-flight write controller');
  const signedOutCalls = app.calls.length;
  releaseLate(); await idle(app);
  assert.equal(app.calls.length, signedOutCalls, 'late completion cannot trigger a new authenticated refresh');
  assert.equal(get('auth').hidden, false);
  assert.equal(get('workspace').hidden, true);
  assert.equal(app.timers.size, 0);
  assert.equal(get('action-status').hidden, true, 'late completion cannot restore a success notice');
  assert.equal(get('claims').textContent, '', 'late completion cannot restore private claims');
  assert.equal(app.root.querySelectorAll('[data-write]').length, 0);
  assert.ok(![...app.storage.values()].includes(input.tokens.a), 'logout leaves no persisted agent credential');
""", seed)
