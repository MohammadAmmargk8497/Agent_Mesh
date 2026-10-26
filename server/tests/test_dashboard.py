"""Packaging and browser-safety contracts shared by both dashboard hosts."""

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DASHBOARD = ROOT / "cloudflare/src/dashboard.html"
SERVER_DASHBOARD = ROOT / "server/src/agentmesh_server/dashboard.html"


class DashboardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.tree = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.tree]

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)


def dashboard_document():
    html = WORKER_DASHBOARD.read_text(encoding="utf-8")
    document = DashboardParser()
    document.feed(html)
    return html, document


def test_dashboard_copies_are_byte_identical():
    assert WORKER_DASHBOARD.read_bytes() == SERVER_DASHBOARD.read_bytes()


def test_dashboard_loads_no_external_cdn_or_other_remote_assets():
    html, document = dashboard_document()
    for tag, attrs in document.elements:
        # External navigation links are not loaded assets; resource hints are.
        urls = [attrs.get("src", ""), attrs.get("srcset", ""), attrs.get("poster", "")]
        if tag in {"link", "image", "use"}:
            urls.extend([attrs.get("href", ""), attrs.get("xlink:href", "")])
        assert not any(re.search(r"(?:https?:)?//", url, re.IGNORECASE) for url in urls), (tag, attrs)
    assert not re.search(r"(?:@import\b\s*(?:url\(\s*)?|url\(\s*)[\"']?(?:https?:)?//", html, re.IGNORECASE)


def test_dashboard_inline_assets_keep_nonce_and_no_event_attributes():
    _, document = dashboard_document()
    executable = [(tag, attrs) for tag, attrs in document.elements if tag in {"script", "style"}]
    assert any(tag == "script" for tag, _ in executable)
    assert any(tag == "style" for tag, _ in executable)
    assert all(attrs.get("nonce") == "__AGENTMESH_NONCE__" for _, attrs in executable)
    assert all("src" not in attrs for tag, attrs in executable if tag == "script")
    for _, attrs in document.elements:
        assert not any(name.lower().startswith("on") for name in attrs)


def test_dashboard_uses_text_nodes_not_html_insertion_for_transcript():
    html, _ = dashboard_document()
    scripts = "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.DOTALL | re.IGNORECASE))
    assert "textContent" in scripts
    assert not re.search(r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|createContextualFragment)\b", scripts)
    assert not re.search(r"\bdocument\s*\.\s*(?:write|writeln)\s*\(", scripts)


def test_dashboard_keeps_writable_access_controls_and_theme():
    html, document = dashboard_document()
    ids = {attrs["id"]: (tag, attrs) for tag, attrs in document.elements if "id" in attrs}
    for control in ("theme-toggle", "sign-out", "access-form", "access-key", "auth-error", "connection-status"):
        assert control in ids
    assert ids["access-key"][1].get("type") == "password"
    assert ids["access-key"][1].get("autocomplete") == "off"
    assert '"/v1/dashboard/state?after="' in html
    assert '/api/legacy/transcript' in html
    assert 'access-mode' not in ids
    assert 'viewer-form' not in ids
    assert 'viewer-key' not in ids
    assert not re.search(r'read[- ]only|value=[\"\']viewer[\"\']', html, re.IGNORECASE)
    assert 'Authorization: "Bearer "' in html
    # Notion palette: light/dark background and text, accent, agent-a/agent-b colors.
    for color in ("#f7f7f5", "#37352f", "#2383e2", "#d9730d", "#191919", "#529cca", "#ffa344"):
        assert color in html.lower()


def test_dashboard_has_coordination_panels_and_huddle_navigation():
    _, document = dashboard_document()
    ids = {attrs["id"] for _, attrs in document.elements if "id" in attrs}
    assert {
        "members", "rules", "asks", "questions", "question-count", "tasks",
        "master", "prompt-badge", "huddles", "huddle-list", "huddle-view",
        "claims", "news", "messages", "patches", "audit",
    } <= ids


def test_dashboard_openhuddle_attribution_includes_mit_notice():
    attribution = (ROOT / "docs/THIRD_PARTY.md").read_text(encoding="utf-8")
    assert "https://github.com/nika110/openhuddle" in attribution
    for source in ("dashboard.html", "dashboard.js", "dashboard.css", "base.css"):
        assert "src/hub/pages/" + source in attribution
    assert "Copyright (c) 2026 nika110" in attribution
    assert "Permission is hereby granted, free of charge" in attribution
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in attribution
    assert "SOFTWARE." in attribution


# A small DOM model runs the actual inline scripts without a browser dependency.
# This checks data binding and session behavior, not CSS/layout rendering.
BROWSER_SMOKE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

function browser(snapshot) {
  const ids = new Map(), timers = new Map(), storage = new Map([['papers-theme', 'dark']]);
  let document, timerId = 0, nextStatus = 200, deferred = null;
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {};
      this.dataset = {}; this.listeners = {}; this.hidden = false; this.open = false;
      this.value = ''; this.className = ''; this.scrollTop = 0;
      this.scrollHeight = 100; this.clientHeight = 100; this.ownText = '';
    }
    set textContent(value) { this.ownText = String(value); this.children = []; }
    get textContent() { return this.ownText + this.children.map(x => x.textContent).join(''); }
    set id(value) { this._id = String(value); ids.set(this._id, this); }
    get id() { return this._id || ''; }
    get childElementCount() { return this.children.filter(node => !node.tagName.startsWith('#')).length; }
    set innerHTML(value) { throw new Error('Unsafe HTML insertion: ' + value); }
    append(...nodes) {
      for (let node of nodes) {
        if (typeof node === 'string') { const text = new Element('#text'); text.textContent = node; node = text; }
        if (node.tagName === '#FRAGMENT') { this.append(...node.children); continue; }
        node.parentElement = this; this.children.push(node);
      }
    }
    prepend(...nodes) { const previous = this.children; this.children = []; this.append(...nodes, ...previous); }
    replaceChildren(...nodes) { this.ownText = ''; this.children = []; this.append(...nodes); }
    setAttribute(name, value) {
      this.attrs[name] = String(value);
      if (name === 'id') { this.id = String(value); ids.set(this.id, this); }
      if (name === 'class') this.className = String(value);
      if (name === 'hidden') this.hidden = true;
      if (name === 'open') this.open = true;
      if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, x) => x.toUpperCase())] = String(value);
    }
    getAttribute(name) {
      if (name.startsWith('data-')) return this.dataset[name.slice(5).replace(/-([a-z])/g, (_, x) => x.toUpperCase())] ?? null;
      if (name === 'class') return this.className || null;
      return this.attrs[name] ?? null;
    }
    removeAttribute(name) { delete this.attrs[name]; if (name === 'hidden' || name === 'open') this[name] = false; }
    addEventListener(name, callback) { (this.listeners[name] ??= []).push(callback); }
    fire(name) {
      const event = {target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}};
      for (const callback of this.listeners[name] || []) callback(event);
    }
    click() { this.fire('click'); }
    focus() { document.activeElement = this; }
    contains(node) { return node === this || this.children.some(x => x.contains(node)); }
    matches(selector) {
      const tag = selector.match(/^[a-z][\w-]*/i);
      if (tag && this.tagName !== tag[0].toUpperCase()) return false;
      for (const match of selector.matchAll(/\.([\w-]+)/g)) if (!this.className.split(/\s+/).includes(match[1])) return false;
      for (const match of selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)) {
        const value = match[1] === 'open' ? (this.open ? '' : null) : this.getAttribute(match[1]);
        if (value === null || (match[2] !== undefined && value !== match[2])) return false;
      }
      return true;
    }
    querySelectorAll(selector) {
      const result = [];
      for (const child of this.children) {
        if (selector.split(',').some(part => child.matches(part.trim()))) result.push(child);
        result.push(...child.querySelectorAll(selector));
      }
      return result;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }
  function build(tree) {
    if (typeof tree === 'string') { const text = new Element('#text'); text.textContent = tree; return text; }
    const node = new Element(tree.tag);
    for (const [name, value] of Object.entries(tree.attrs)) node.setAttribute(name, value ?? '');
    for (const child of tree.children) node.append(build(child));
    return node;
  }
  const root = build(input.tree), get = id => {
    const node = ids.get(id); return node && root.contains(node) ? node : null;
  };
  document = {
    documentElement: root.querySelector('html'), activeElement: null,
    getElementById: get, createElement: tag => new Element(tag),
    createDocumentFragment: () => new Element('#fragment'),
    querySelectorAll: selector => root.querySelectorAll(selector), addEventListener() {},
  };
  const calls = [];
  const context = vm.createContext({
    document, AbortController, console,
    localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    setInterval: callback => { const id = ++timerId; timers.set(id, callback); return id; },
    clearInterval: id => timers.delete(id),
    fetch: async (url, options) => {
      calls.push({url, options}); const status = nextStatus; nextStatus = 200;
      const pending = deferred; deferred = null; if (pending) await pending;
      return {status, ok: status === 200, json: async () => structuredClone(snapshot)};
    },
  });
  for (const script of input.scripts) vm.runInContext(script, context, {timeout: 3000});
  return {
    get, root, document, calls, storage, timers, setStatus: value => { nextStatus = value; },
    deferNext: () => { let release; deferred = new Promise(resolve => { release = resolve; }); return release; },
  };
}

const flush = () => new Promise(resolve => setImmediate(resolve));
async function login(app) {
  app.get('access-key').value = 'a'.repeat(40);
  app.get('access-form').fire('submit');
  await flush();
}

(async () => {
  const app = browser(input.snapshot), get = app.get;
  assert.equal(app.calls.length, 0, 'no authenticated request before a credential');
  assert.equal(get('workspace').hidden, true);
  assert.equal(app.document.documentElement.getAttribute('data-theme'), 'dark');
  get('theme-toggle').click();
  assert.equal(app.document.documentElement.getAttribute('data-theme'), 'light');
  assert.equal(app.storage.get('papers-theme'), 'light');
  get('access-key').value = 'short'; get('access-form').fire('submit');
  assert.equal(app.calls.length, 0, 'short keys must not make a request');
  assert.equal(get('auth-error').hidden, false);
  await login(app);
  assert.match(get('connection-status').textContent, /Live/);
  assert.equal(get('workspace').hidden, false);
  assert.equal(app.calls[0].url, '/v1/dashboard/state?after=0');
  assert.equal(app.calls[0].options.headers.Authorization, 'Bearer ' + 'a'.repeat(40));
  assert.equal(app.calls[0].options.cache, 'no-store');
  assert.equal(app.calls[0].options.method ?? 'GET', 'GET');
  for (const id of ['messages', 'tasks', 'master', 'huddle-list', 'claims', 'questions', 'news', 'patches']) {
    assert.ok(get(id).textContent.includes(input.unsafe), id + ' must preserve untrusted strings as literal text');
    assert.equal(get(id).querySelectorAll('img').length, 0, id + ' must not create injected image nodes');
  }
  assert.equal(get('tasks').querySelectorAll('.board-column').length, 4);
  assert.match(get('master').textContent, /agent-b/);
  assert.match(get('questions').textContent, /agent-b/);
  assert.ok(get('questions').textContent.includes(input.long_question), 'questions retain the complete question text');
  const attentionQuestion = get('asks-list').querySelectorAll('li').find(node => node.textContent.includes(' asked “'));
  assert.ok(attentionQuestion, 'unanswered questions appear in the attention banner');
  const preview = attentionQuestion.textContent.match(/asked “([\s\S]+)” · waiting on/)[1];
  assert.ok(preview.includes(input.long_question.slice(0, 120)), 'attention preview keeps the start of the question');
  assert.ok(preview.length <= 143, 'attention preview is limited to 140 characters plus an ellipsis');
  assert.match(preview, /…|\.\.\./);
  assert.ok(!get('asks-list').textContent.includes(input.long_question), 'attention banner does not repeat the full long question');
  const openHuddle = get('huddle-list').querySelector('button');
  assert.ok(openHuddle, 'huddle has a keyboard-native navigation button');
  openHuddle.click();
  assert.equal(get('huddle-view').hidden, false);
  assert.ok(get('huddle-view').textContent.includes(input.unsafe));
  assert.match(get('huddle-view').textContent, /Back it/);
  assert.match(get('huddle-view').textContent, /Push back/);
  const ideas = get('huddle-view').querySelectorAll('.idea');
  assert.deepEqual(ideas.map(node => node.querySelector('.idea-top').querySelector('.meta').textContent), ['#1', '#2']);
  assert.equal(ideas[0].querySelector('.backing').textContent, 'Back it +1');
  assert.equal(ideas[0].querySelector('.pushback').textContent, 'Push back −1');
  assert.equal(ideas[1].querySelector('.backing').textContent, 'Back it +1');
  assert.equal(ideas[1].querySelector('.pushback').textContent, 'Push back −0');
  const discussionPosts = get('huddle-view').querySelectorAll('.talk-item');
  assert.equal(discussionPosts.length, 5);
  assert.equal(discussionPosts[3].querySelector('.meta').textContent, 'Post #4');
  assert.match(discussionPosts[4].textContent, /on idea #2/);
  get('huddle-back').click();
  assert.equal(get('huddle-list').hidden, false);
  assert.equal(get('huddle-view').hidden, true);
  assert.equal(app.calls.length, 1, 'huddle navigation needs no additional endpoint');
  const details = get('tasks').querySelector('details'); details.open = true;
  input.snapshot.tasks[0].title += ' updated';
  await [...app.timers.values()][0]();
  assert.match(get('connection-status').textContent, /Live/);
  assert.equal(app.calls[1].url, '/v1/dashboard/state?after=' + input.snapshot.last_event_id);
  assert.equal(get('tasks').querySelector('details').open, true, 'polls preserve expanded task details');
  get('sign-out').click();
  assert.equal(get('workspace').hidden, true);
  assert.equal(get('auth').hidden, false);
  assert.equal(app.storage.has('agentmesh-viewer'), false);
  assert.equal(app.timers.size, 0);
  for (const id of ['messages', 'tasks', 'master', 'huddle-list', 'huddle-view', 'claims', 'questions', 'news', 'members', 'asks-list']) {
    assert.ok(!get(id).textContent.includes(input.unsafe), id + ' must clear on sign out');
  }
  app.setStatus(403); await login(app);
  assert.equal(get('auth').hidden, false);
  assert.equal(app.storage.has('agentmesh-viewer'), false);
  assert.equal(app.timers.size, 0);
  assert.match(get('auth-error').textContent, /member key|agent token/i);
  for (const call of app.calls) {
    assert.match(call.url, /^\/v1\/dashboard\/state\?after=\d+$/);
    assert.equal(call.options.method ?? 'GET', 'GET');
    assert.ok(call.options.headers.Authorization.startsWith('Bearer '));
  }

  const old = browser({me: 'agent-a', messages: [], tasks: [], patches: [], audit: [], events: [], last_event_id: 0});
  await login(old);
  assert.match(old.get('connection-status').textContent, /Live/, 'legacy snapshots must still render');
  const raced = browser(input.snapshot); await login(raced);
  const release = raced.deferNext(), pendingPoll = [...raced.timers.values()][0]();
  raced.get('sign-out').click(); release(); await pendingPoll;
  assert.equal(raced.calls[1].options.signal.aborted, true);
  assert.equal(raced.get('workspace').hidden, true, 'a stale response must not reopen a signed-out session');
  assert.equal(raced.get('messages').textContent, '', 'a stale response must not restore private transcript text');
  console.log('Dashboard data binding, text safety, navigation, theme and agent sessions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def test_dashboard_binds_real_agent_state_and_preserves_session(api):
    unsafe = '<img src=x onerror="globalThis.injected=true"><script>bad()</script>'
    long_question = unsafe + " Please explain the longer implementation details." * 12
    task = api.post("/v1/tasks", {
        "title": unsafe, "description": unsafe, "assignee": "agent-b", "acceptance_criteria": [unsafe],
    })
    assert task.status == 201
    assert api.post("/v1/messages", {"to": "agent-b", "text": unsafe}).status == 201
    assert api.post("/v1/patches", {"title": unsafe, "text": "patch metadata only"}).status == 201
    assert api.post("/v1/master/sections", {"section": "Goal", "text": unsafe}).status == 201
    assert api.post("/v1/master/submit", {}).status == 200
    huddle = api.post("/v1/huddles", {"kind": "brainstorm", "topic": unsafe})
    assert huddle.status == 201
    path = f"/v1/huddles/{huddle.json()['id']}/posts"
    idea = api.post(path, {"kind": "idea", "text": unsafe})
    assert idea.status == 201
    for kind in ("back", "push_back"):
        assert api.post(path, {"kind": kind, "text": unsafe, "ref_post_id": idea.json()["id"]}, "b").status == 201
    second_idea = api.post(path, {"kind": "idea", "text": "Second idea " + unsafe})
    assert second_idea.status == 201
    assert api.post(path, {"kind": "back", "text": unsafe, "ref_post_id": second_idea.json()["id"]}, "b").status == 201
    assert api.post("/v1/claims", {"path": "src/" + unsafe}).status == 201
    assert api.post("/v1/questions", {"to": "agent-b", "text": long_question}).status == 201
    transcript = api.get("/v1/dashboard/state", "a")
    assert transcript.status == 200
    html, document = dashboard_document()
    payload = {
        "scripts": re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.DOTALL | re.IGNORECASE),
        "tree": document.tree, "snapshot": transcript.json(), "unsafe": unsafe, "long_question": long_question,
    }
    result = subprocess.run(
        ["node", "-e", BROWSER_SMOKE], input=json.dumps(payload), text=True,
        capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
