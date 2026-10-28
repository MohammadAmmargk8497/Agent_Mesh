// Execute the shipped inline scripts without adding a browser or DOM dependency.
// The same scenarios run against the real Worker and Python hub implementations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const dashboardPath = path.join(root, 'cloudflare/src/dashboard.html');

function parseDocument(html) {
  const tree = { tag: 'document', attrs: {}, children: [] }, stack = [tree];
  const tokens = /<!--[\s\S]*?-->|<(script|style)\b([^>]*)>[\s\S]*?<\/\1\s*>|<\/?[a-z][^>]*>|[^<]+/gi;
  const voidTags = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
  for (const [token] of html.matchAll(tokens)) {
    if (token.startsWith('<!--')) continue;
    if (token.startsWith('</')) {
      const tag = /^<\/([^\s>]+)/.exec(token)[1].toLowerCase();
      for (let i = stack.length - 1; i > 0; i--) if (stack[i].tag === tag) { stack.length = i; break; }
    } else if (token.startsWith('<')) {
      const match = /^<([^\s>]+)([^>]*)>/.exec(token);
      const node = { tag: match[1].toLowerCase(), attrs: {}, children: [] };
      for (const attr of match[2].matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) node.attrs[attr[1]] = attr[2] ?? attr[3] ?? attr[4] ?? '';
      stack.at(-1).children.push(node);
      if (!voidTags.has(node.tag) && !['script', 'style'].includes(node.tag)) stack.push(node);
    } else stack.at(-1).children.push(token);
  }
  return tree;
}

function browser(input, request, options = {}) {
  const ids = new Map(), timers = new Map(), storage = new Map([['papers-theme', 'dark']]);
  const sessionStorage = options.sessionStorage || new Map(), calls = [], historyCalls = [], clipboard = [], streams = [];
  let document, nextTimer = 0;
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.dataset = {};
      this.listeners = {}; this.hidden = false; this.disabled = false; this.open = false;
      this.value = ''; this.className = ''; this.ownText = ''; this.scrollTop = 0;
      this.scrollHeight = 100; this.clientHeight = 100;
      this.classList = {
        add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' '); },
        remove: (...names) => { this.className = this.className.split(/\s+/).filter(name => !names.includes(name)).join(' '); },
        contains: name => this.className.split(/\s+/).includes(name),
        toggle: (name, force) => { const add = force ?? !this.classList.contains(name); this.classList[add ? 'add' : 'remove'](name); return add; },
      };
    }
    set textContent(value) { this.ownText = String(value); this.children = []; }
    get textContent() { return this.ownText + this.children.map(node => node.textContent).join(''); }
    set innerHTML(value) { throw new Error('Unsafe HTML insertion: ' + value); }
    set outerHTML(value) { throw new Error('Unsafe HTML insertion: ' + value); }
    set id(value) { this._id = String(value); ids.set(this._id, this); }
    get id() { return this._id || ''; }
    get childNodes() { return this.children; }
    get firstChild() { return this.children[0] || null; }
    get firstElementChild() { return this.children.find(node => !node.tagName.startsWith('#')) || null; }
    get childElementCount() { return this.children.filter(node => !node.tagName.startsWith('#')).length; }
    get parentNode() { return this.parentElement; }
    append(...nodes) {
      for (let node of nodes) {
        if (!(node instanceof Element)) { const value = node; node = new Element('#text'); node.textContent = value; }
        if (node.tagName === '#FRAGMENT') { this.append(...node.children); continue; }
        node.parentElement = this; this.children.push(node);
      }
    }
    appendChild(node) { this.append(node); return node; }
    prepend(...nodes) { const old = this.children; this.children = []; this.append(...nodes, ...old); }
    replaceChildren(...nodes) { this.ownText = ''; this.children = []; this.append(...nodes); }
    replaceWith(node) {
      const siblings = this.parentElement.children, i = siblings.indexOf(this);
      siblings.splice(i, 1, node); node.parentElement = this.parentElement;
    }
    remove() { this.parentElement.children = this.parentElement.children.filter(node => node !== this); }
    setAttribute(name, value) {
      this.attrs[name] = String(value);
      if (name === 'id') this.id = value;
      if (name === 'class') this.className = String(value);
      if (['value', 'type', 'name'].includes(name)) this[name] = String(value);
      if (['hidden', 'open', 'disabled', 'checked'].includes(name)) this[name] = true;
      if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = String(value);
    }
    getAttribute(name) {
      if (name === 'id') return this.id || null;
      if (name === 'class') return this.className || null;
      if (['hidden', 'open', 'disabled', 'checked'].includes(name)) return this[name] ? '' : null;
      if (name.startsWith('data-')) return this.dataset[name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] ?? null;
      return this.attrs[name] ?? null;
    }
    removeAttribute(name) { delete this.attrs[name]; if (['hidden', 'open', 'disabled', 'checked'].includes(name)) this[name] = false; }
    addEventListener(name, callback) { (this.listeners[name] ??= []).push(callback); }
    async fire(name, extras = {}) {
      const event = { target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...extras };
      await Promise.all((this.listeners[name] || []).map(callback => callback(event)));
    }
    click() { if (!this.disabled) return this.fire('click'); }
    focus() { document.activeElement = this; }
    blur() { if (document.activeElement === this) document.activeElement = null; }
    select() { this.focus(); }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    reset() { for (const node of this.querySelectorAll('input,textarea,select')) { node.value = node.getAttribute('value') || ''; node.checked = false; } }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
    matches(selector) {
      const parts = selector.trim().split(/\s+(?![^\[]*\])/), tail = parts.pop();
      const tag = tail.match(/^[a-z][\w-]*/i);
      if (tag && this.tagName !== tag[0].toUpperCase()) return false;
      const id = tail.match(/#([\w-]+)/); if (id && this.id !== id[1]) return false;
      for (const [, name] of tail.matchAll(/\.([\w-]+)/g)) if (!this.classList.contains(name)) return false;
      for (const [, name, expected] of tail.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)) {
        const value = this.getAttribute(name);
        if (value === null || (expected !== undefined && value !== expected)) return false;
      }
      if (parts.length) {
        const parentSelector = parts.join(' '); let node = this.parentElement;
        while (node) { if (node.matches(parentSelector)) return true; node = node.parentElement; }
        return false;
      }
      return true;
    }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    querySelectorAll(selector) {
      const found = [];
      for (const child of this.children) {
        if (selector.split(',').some(part => child.matches(part))) found.push(child);
        found.push(...child.querySelectorAll(selector));
      }
      return found;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }
  function build(tree) {
    if (typeof tree === 'string') { const node = new Element('#text'); node.textContent = tree; return node; }
    const node = new Element(tree.tag);
    for (const [name, value] of Object.entries(tree.attrs)) node.setAttribute(name, value ?? '');
    for (const child of tree.children) node.append(build(child));
    return node;
  }
  const dom = build(input.tree), get = id => { const node = ids.get(id); return node && dom.contains(node) ? node : null; };
  document = {
    documentElement: dom.querySelector('html'), activeElement: null, title: '',
    getElementById: get, createElement: tag => new Element(tag),
    createTextNode: value => { const node = new Element('#text'); node.textContent = value; return node; },
    createDocumentFragment: () => new Element('#fragment'),
    querySelector: selector => dom.querySelector(selector), querySelectorAll: selector => dom.querySelectorAll(selector), addEventListener() {},
  };
  const location = { pathname: options.pathname || '/t/' + input.team, search: '', hash: options.hash || '', href: 'https://agentmesh.example', replace() {} };
  const store = data => ({ getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) });
  const app = { get, root: dom, document, storage, sessionStorage, calls, timers, historyCalls, clipboard, location, stateTransform: null, statusOverride: null,
    sendChunk: chunk => { for (const stream of streams) stream.push(chunk); } };
  const context = vm.createContext({
    document, Node: Element, AbortController, URL, URLSearchParams, TextDecoder, TextEncoder, structuredClone, console,
    Date: options.now === undefined ? Date : class extends Date { static now() { return options.now; } },
    location, history: { replaceState: (...args) => { historyCalls.push(args); location.hash = ''; } },
    navigator: { clipboard: { writeText: async text => { clipboard.push(text); } } },
    localStorage: store(storage), sessionStorage: store(sessionStorage),
    setInterval: callback => { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearInterval: id => timers.delete(id),
    setTimeout: callback => { const id = ++nextTimer; return id; }, clearTimeout() {},
    fetch: async (url, init = {}) => {
      const call = { url: String(url), options: init, done: false }; calls.push(call);
      try {
        // Streaming itself is separately tested against both server runtimes. Keep
        // the browser reader open without a network handle or reconnect spin.
        if (call.url.startsWith('/api/stream')) {
          let waiting = null, closed = false; const chunks = [];
          const close = () => { closed = true; if (waiting) { waiting({ done: true }); waiting = null; } };
          const reader = { read: () => closed ? Promise.resolve({ done: true }) : chunks.length ? Promise.resolve(chunks.shift()) : new Promise(resolve => { waiting = resolve; }),
            cancel: async () => close() };
          streams.push({ push(chunk) {
            if (closed) return;
            const value = { done: false, value: new TextEncoder().encode(chunk) };
            if (waiting) { const resolve = waiting; waiting = null; resolve(value); } else chunks.push(value);
          } });
          init.signal?.addEventListener('abort', close);
          return { status: 200, ok: true, body: { getReader: () => reader, cancel: async () => close() } };
        }
        if (app.statusOverride) {
          const status = app.statusOverride; app.statusOverride = null;
          call.status = status; return { status, ok: false, json: async () => ({ error: 'Access denied' }) };
        }
        const response = await request(call.url, init), payload = await response.json();
        call.status = response.status; call.payload = payload;
        if (call.url === '/api/state' && app.stateTransform) app.stateTransform(payload);
        return { status: response.status, ok: response.ok, json: async () => structuredClone(payload) };
      } finally { call.done = true; }
    },
  });
  for (const script of input.scripts) vm.runInContext(script, context, { timeout: 3000 });
  return app;
}

const flush = () => new Promise(resolve => setTimeout(resolve, 10));
async function idle(app) {
  for (let i = 0; i < 1000; i++) {
    await flush();
    if (app.calls.every(call => call.done)) { const count = app.calls.length; await flush(); if (count === app.calls.length) return; }
  }
  assert.fail('Browser requests did not settle: ' + app.calls.filter(call => !call.done).map(call => call.url));
}
async function login(app, token) {
  app.get('access-key').value = token;
  await app.get('access-form').fire('submit'); await idle(app);
  assert.equal(app.get('auth').hidden, true, app.get('auth-error').textContent);
}
async function poll(app) {
  for (const refresh of app.timers.values()) await refresh();
  await idle(app);
}
function findButton(container, text) {
  const buttons = container.querySelectorAll('button').filter(node => node.textContent.trim() === text);
  assert.equal(buttons.length, 1, 'one button labelled ' + text + ' in ' + container.id);
  return buttons[0];
}
async function click(app, button) {
  assert.equal(Boolean(button.disabled), false, button.textContent + ' is enabled');
  const before = app.calls.length; await button.click(); await idle(app);
  return app.calls.slice(before);
}
async function set(app, id, value) {
  const node = app.get(id); assert.ok(node, 'field exists: ' + id); node.value = value;
  await node.fire('input'); await node.fire('change'); return node;
}
function postFrom(calls, route) {
  const writes = calls.filter(call => call.url === route && call.options.method === 'POST');
  assert.equal(writes.length, 1, 'one write to ' + route);
  assert.ok(writes[0].status >= 200 && writes[0].status < 300, route + ': ' + JSON.stringify(writes[0].payload));
  return JSON.parse(writes[0].options.body);
}

async function run(request, tokens) {
  const dashboard = fs.readFileSync(dashboardPath), html = dashboard.toString('utf8');
  assert.deepEqual(dashboard, fs.readFileSync(path.join(root, 'server/src/agentmesh_server/dashboard.html')), 'both servers ship byte-identical pages');
  const input = { tree: parseDocument(html), scripts: [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)].map(match => match[1]) };
  async function api(route, body, token = tokens.a) {
    const response = await request(route, { headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }) });
    const data = await response.json(); assert.equal(response.status, 200, route + ': ' + JSON.stringify(data)); return data;
  }
  const initial = await api('/api/state'); input.team = initial.team.id;
  await runTranscript(input, request, api, tokens);
  await runSessions(input, request, tokens, html);
  const empty = browser(input, request); await idle(empty);
  assert.equal(empty.calls.length, 0, 'no authenticated requests before a key is supplied');
  assert.equal(empty.get('access-mode'), null, 'one access form has no viewer or mode selector');
  assert.doesNotMatch(html, /read[- ]only|value=["']viewer["']/i, 'the served page has no read-only mode');
  for (const id of ['tasks', 'huddle-list', 'huddle-view', 'master', 'news', 'members', 'rules', 'house-rules', 'questions', 'claims',
    'messages', 'audit', 'patches', 'coordination', 'theme-toggle', 'sign-out', 'member-heartbeats', 'invite-show']) assert.ok(empty.get(id), 'panel/control ' + id);
  assert.equal(empty.document.documentElement.getAttribute('data-theme'), 'dark', 'dashboard theme preference retained');
  await empty.get('theme-toggle').click();
  assert.equal(empty.document.documentElement.getAttribute('data-theme'), 'light');
  assert.equal(empty.storage.get('papers-theme'), 'light');

  const app = browser(input, request, { hash: '#k=' + encodeURIComponent(tokens.a) }); await idle(app);
  assert.equal(app.get('auth').hidden, true, 'fragment member key opens the unified dashboard');
  assert.equal(app.location.hash, '', 'fragment credential removed from the URL');
  assert.ok(app.historyCalls.length, 'credential fragment removal uses history replacement');
  assert.ok(app.calls.some(call => call.url === '/api/state'));
  assert.ok(app.calls.some(call => call.url.startsWith('/api/events')), 'hub live feed is loaded');
  assert.ok(app.calls.some(call => call.url.startsWith('/api/legacy/transcript?') && new URL(call.url, 'https://example').searchParams.get('team') === input.team), 'legacy panels read the explicitly scoped adapter');
  assert.ok(!app.calls.some(call => call.url.startsWith('/v1/')), 'member mode never mixes legacy bearer endpoints into hub state');
  for (const call of app.calls) assert.equal(call.options.headers.Authorization, 'Bearer ' + tokens.a, call.url + ' uses the same member key');
  assert.ok(![...app.storage.values()].includes(tokens.a), 'member key is not saved in localStorage');
  assert.doesNotMatch(app.root.querySelector('body').textContent, /openhuddle/i, 'no upstream branding in visible UI');
  for (const token of Object.values(tokens)) assert.ok(!html.includes(token), 'no credential embedded in HTML');
  const event = { id: initial.last_event_id + 1000, member_id: initial.me.id, kind: 'update', data: { text: 'An update delivered over the live stream' }, created_at: Date.now() };
  const eventText = 'data: ' + JSON.stringify(event) + '\r\n\r\n';
  app.sendChunk(eventText.slice(0, 20)); app.sendChunk(eventText.slice(20)); await idle(app);
  assert.ok(app.get('news').textContent.includes(event.data.text), 'split SSE events update the live feed');
  const inviteCalls = await click(app, app.get('invite-show'));
  postFrom(inviteCalls, '/api/invite');
  assert.match(app.get('invite-url').value, new RegExp('/join/' + input.team + '\\.'));
  assert.equal(app.get('invite-box').hidden, false);
  await click(app, app.get('invite-copy'));
  assert.deepEqual(app.clipboard, [app.get('invite-url').value]);
  await app.get('sign-out').click(); await idle(app);
  assert.equal(app.get('auth').hidden, false); assert.equal(app.get('workspace').hidden, true);
  assert.equal(app.get('member-heartbeats').textContent, '', 'sign-out clears other members');
  assert.equal(app.timers.size, 0, 'sign-out stops polling');
  assert.equal(app.sessionStorage.has('agentmesh:session'), false, 'sign-out removes remembered member session');
  await login(app, tokens.a);

  const rootApp = browser(input, request, { pathname: '/', hash: '#k=' + encodeURIComponent(tokens.a) }); await idle(rootApp);
  assert.equal(rootApp.get('auth').hidden, true, 'the agentmesh root discovers the member team without a /t URL');
  assert.ok(rootApp.get('members').textContent.includes('agent-a'));
  assert.ok(rootApp.calls.some(call => call.url === '/api/state'), 'fragment credentials retain member-key access');

  const agent = browser(input, request, { pathname: '/' }); await login(agent, tokens.a);
  assert.match(agent.get('connection-status').textContent, /agent-a/, 'typed legacy token selects its agent identity');
  assert.ok(agent.calls.some(call => call.url === '/v1/dashboard/state?after=0' && call.status === 200));
  assert.ok(agent.calls.some(call => call.url.startsWith('/api/legacy/transcript?') && call.status === 200), 'agent controls use the separately member-authorized team transcript');
  assert.ok(agent.calls.filter(call => call.url.startsWith('/api/')).every(call => (call.options.method || 'GET') === 'GET'), 'agent controls only read the member transcript and never mutate through member routes');
  assert.deepEqual(JSON.parse(agent.sessionStorage.get('agentmesh:session')), { key: tokens.a, mode: 'auto' }, 'typed agent token is remembered with its detection mode');
  assert.ok(![...agent.storage.values()].some(value => value.includes(tokens.a)), 'legacy agent credential is never saved in localStorage');
  for (const enabled of [false, true]) {
    const before = agent.calls.length, toggle = agent.get('auto-respond');
    assert.ok(toggle, 'agent settings retain a writable control'); toggle.checked = enabled; await toggle.fire('change'); await idle(agent);
    assert.deepEqual(postFrom(agent.calls.slice(before), '/v1/settings'), { auto_respond: enabled });
  }
  await agent.get('sign-out').click(); await idle(agent);
  const rejected = browser(input, request, { pathname: '/' });
  rejected.get('access-key').value = tokens.viewer; await rejected.get('access-form').fire('submit'); await idle(rejected);
  assert.equal(rejected.get('auth').hidden, false, 'removed viewer credential cannot authenticate');
  assert.equal(rejected.get('workspace').hidden, true);
  assert.equal(rejected.sessionStorage.has('agentmesh:session'), false, 'failed typed authentication clears the saved session');
  assert.equal(rejected.root.querySelectorAll('[data-write]').length, 0, 'rejected viewer token exposes no writes');
  assert.ok(rejected.calls.every(call => (call.options.method || 'GET') === 'GET'), 'rejected viewer never causes a write');
  assert.ok(!rejected.calls.some(call => call.url.startsWith('/v1/transcript')), 'removed viewer read path is never requested');

  let releaseSnapshot;
  const snapshotGate = new Promise(resolve => { releaseSnapshot = resolve; });
  const stale = browser(input, async (route, options) => {
    // Model a transport that has already received its response when abort runs:
    // the session-generation guard, not cancellation alone, must hide its data.
    const response = await request(route, { ...options, signal: undefined });
    if (route === '/api/state') await snapshotGate;
    return response;
  }, { hash: '#k=' + encodeURIComponent(tokens.a) });
  await stale.get('sign-out').click(); releaseSnapshot(); await idle(stale);
  assert.equal(stale.calls.find(call => call.url === '/api/state').payload.team.id, input.team, 'a real late snapshot was delivered');
  assert.equal(stale.get('auth').hidden, false, 'a late pre-sign-out snapshot never reconnects the page');
  assert.equal(stale.get('members').textContent, ''); assert.equal(stale.get('member-heartbeats').textContent, '');
  assert.equal(stale.timers.size, 0);

  const now = Date.now(), presence = browser(input, request, { now });
  presence.stateTransform = state => { state.members = [
    { ...state.members[0], name: '<img src=x onerror=alert(1)>', last_seen: now - 120000 },
    { ...state.members[1], name: 'Just away', last_seen: now - 120001 },
    { ...state.members[1], id: 'boundary-away', name: 'Away boundary', last_seen: now - 900000 },
    { ...state.members[1], id: 'offline', name: 'Offline member', last_seen: now - 900001 },
    { ...state.members[1], id: 'unknown', name: 'Never seen', last_seen: null },
  ]; };
  await login(presence, tokens.a);
  const chips = presence.get('member-heartbeats'); assert.equal(chips.children.length, 5, 'one heartbeat chip per member');
  for (const [i, status] of ['online', 'away', 'away', 'offline', 'offline'].entries()) {
    assert.ok(chips.children[i].classList.contains(status) || chips.children[i].dataset.state === status, 'presence threshold for member ' + i + ': ' + chips.children[i].textContent);
    assert.match(chips.children[i].textContent.toLowerCase(), /last seen|last-seen/, 'last-seen context is visible for every heartbeat');
  }
  assert.ok(chips.textContent.includes('<img src=x onerror=alert(1)>'), 'untrusted member names render literally');
  assert.equal(chips.querySelectorAll('img').length, 0, 'member names never become DOM markup');

  // A separate team must never receive the default team's transcript/audit/patches.
  const created = await api('/api/teams', { team_name: 'Isolated test team', member_name: 'Isolated owner' });
  await api('/api/post', { kind: 'update', text: 'Isolated team transcript post' }, created.token);
  const isolated = browser({ ...input, team: created.team_id }, request);
  await login(isolated, created.token);
  assert.equal(isolated.get('auth').hidden, true, 'a valid member can read their own team transcript');
  assert.ok(isolated.get('messages').textContent.includes('Isolated team transcript post'), 'other teams get their own hub messages');
  assert.ok(!isolated.get('messages').textContent.includes('Transcript ask for the humans'), 'another team cannot see the default team ask');
  assert.ok(isolated.get('members').textContent.includes('Isolated owner'));
  assert.ok(!isolated.get('members').textContent.includes('agent-a'), 'presence is scoped to the caller team');
  for (const id of ['messages', 'audit', 'patches']) assert.ok(!isolated.get(id).textContent.includes('agent-a'), id + ' does not leak default-team data');
  const isolatedAdapters = isolated.calls.filter(call => call.url.startsWith('/api/legacy/'));
  assert.equal(isolatedAdapters.length, 1, 'other-team legacy panels use the scoped adapter rather than direct /v1 data');
  assert.ok(isolatedAdapters.every(call => call.status === 200 && new URL(call.url, 'https://example').searchParams.get('team') === created.team_id));
  const typedMember = browser(input, request, { pathname: '/' }); await login(typedMember, created.token);
  assert.ok(typedMember.calls.some(call => call.url.startsWith('/v1/dashboard/state') && call.status === 401), 'member key is not accepted as a legacy agent token');
  assert.ok(typedMember.calls.some(call => call.url === '/api/state' && call.status === 200), 'typed member key opens its own team workspace');
  assert.ok(typedMember.get('members').textContent.includes('Isolated owner'));
  assert.deepEqual(JSON.parse(typedMember.sessionStorage.get('agentmesh:session')), { key: created.token, mode: 'auto' }, 'typed member key is remembered with its detection mode');
  const reloadedMember = browser(input, request, { pathname: '/', sessionStorage: typedMember.sessionStorage }); await idle(reloadedMember);
  assert.equal(reloadedMember.get('auth').hidden, true, 'auto-detected member key resumes on reload');
  assert.ok(reloadedMember.get('members').textContent.includes('Isolated owner'));
  assert.ok(reloadedMember.calls.some(call => call.url.startsWith('/v1/dashboard/state') && call.status === 401));
  assert.ok(reloadedMember.calls.some(call => call.url === '/api/state' && call.status === 200));
  await set(typedMember, 'hub-claim-path', 'src/typed-member.txt');
  const memberBefore = typedMember.calls.length;
  await typedMember.get('hub-claim-form').fire('submit'); await idle(typedMember);
  assert.deepEqual(postFrom(typedMember.calls.slice(memberBefore), '/api/claims').paths, ['src/typed-member.txt'], 'typed member key writes within the detected team');
  await typedMember.get('sign-out').click(); await idle(typedMember);
  assert.equal(typedMember.sessionStorage.has('agentmesh:session'), false, 'sign-out clears the typed member session');
  const wrongTeam = browser(input, request, { hash: '#k=' + encodeURIComponent(created.token) }); await idle(wrongTeam);
  assert.equal(wrongTeam.get('auth').hidden, false, 'a key cannot open another team URL');
  assert.ok(!wrongTeam.get('members').textContent.includes('Isolated owner'));
  assert.equal(wrongTeam.get('member-heartbeats').textContent, '');

  await runActions(input, request, api, tokens);
  console.log('Unified dashboard panels, member sessions, hub actions, presence, safe names and team scoping passed');
}

async function runSessions(input, request, tokens, html) {
  const sessionKey = 'agentmesh:session';
  function assertPrivate(app, token) {
    assert.ok(!html.includes(token), 'credential is not embedded in the served HTML');
    assert.ok(!app.root.textContent.includes(token), 'credential is not rendered as page text');
    function check(node) {
      assert.ok(!node.value.includes(token), 'credential is not retained in a DOM field');
      assert.ok(!Object.values(node.attrs).some(value => value.includes(token)), 'credential is not embedded in a DOM attribute');
      node.children.forEach(check);
    }
    check(app.root);
    assert.ok(![...app.storage.values()].some(value => value.includes(token)), 'credential is never copied into localStorage');
  }
  function assertSession(app, token, mode) {
    assert.deepEqual([...app.sessionStorage.keys()], [sessionKey], 'one session entry stores the key and mode');
    assert.deepEqual(JSON.parse(app.sessionStorage.get(sessionKey)), { key: token, mode });
    assertPrivate(app, token);
  }
  for (const mode of ['member', 'agent', 'auto']) {
    const options = mode === 'member' ? { hash: '#k=' + encodeURIComponent(tokens.a) } : { pathname: '/' };
    // Explicit agent mode is available when restoring a previously saved session;
    // the form deliberately offers no mode selector and uses auto detection.
    if (mode === 'agent') options.sessionStorage = new Map([[sessionKey, JSON.stringify({ key: tokens.a, mode })]]);
    const first = browser(input, request, options);
    if (mode === 'auto') await login(first, tokens.a); else await idle(first);
    assert.equal(first.get('auth').hidden, true, mode + ' session authenticates');
    assertSession(first, tokens.a, mode);

    const reload = browser(input, request, { pathname: first.location.pathname, sessionStorage: first.sessionStorage });
    await idle(reload);
    assert.equal(reload.get('auth').hidden, true, mode + ' session resumes without entering a key');
    assert.equal(reload.get('workspace').hidden, false);
    assert.equal(reload.calls[0].url, mode === 'member' ? '/api/state' : '/v1/dashboard/state?after=0', 'reload honors the saved ' + mode + ' mode');
    for (const call of reload.calls) assert.equal(call.options.headers.Authorization, 'Bearer ' + tokens.a);
    if (mode === 'member') assert.ok(reload.calls.every(call => !call.url.startsWith('/v1/')));
    else {
      assert.ok(reload.calls.some(call => call.url.startsWith('/api/legacy/transcript?') && call.status === 200), 'restored agent session reads the member-authorized transcript');
      assert.ok(reload.calls.filter(call => call.url.startsWith('/api/')).every(call => call.url === '/api/state' || call.url.startsWith('/api/legacy/transcript?')), 'agent sessions use member routes only for the human transcript');
    }
    assertSession(reload, tokens.a, mode);

    await reload.get('sign-out').click(); await idle(reload);
    assert.equal(reload.sessionStorage.has(sessionKey), false, mode + ' sign-out removes the saved session');
    const signedOut = browser(input, request, { pathname: reload.location.pathname, sessionStorage: reload.sessionStorage });
    await idle(signedOut);
    assert.equal(signedOut.calls.length, 0, 'reload after ' + mode + ' sign-out makes no authenticated request');
    assert.equal(signedOut.get('auth').hidden, false);
    assert.equal(signedOut.get('workspace').hidden, true);
    assertPrivate(signedOut, tokens.a);
  }

  const fragment = browser(input, request, { hash: '#k=' + encodeURIComponent(tokens.a),
    sessionStorage: new Map([[sessionKey, JSON.stringify({ key: tokens.b, mode: 'agent' })]]) });
  await idle(fragment);
  assert.equal(fragment.get('auth').hidden, true, 'fragment wins over a saved session');
  assert.equal(fragment.calls[0].url, '/api/state', 'fragment always uses member mode');
  assert.ok(fragment.calls.every(call => call.options.headers.Authorization === 'Bearer ' + tokens.a), 'saved credential is not used when a fragment is supplied');
  assert.equal(fragment.location.hash, '', 'fragment credential is removed from the URL');
  assert.ok(fragment.historyCalls.length);
  assertSession(fragment, tokens.a, 'member');

  for (const mode of ['member', 'agent', 'auto']) {
    const rejected = browser(input, request, { pathname: '/',
      sessionStorage: new Map([[sessionKey, JSON.stringify({ key: tokens.viewer, mode })]]) });
    await idle(rejected);
    assert.equal(rejected.get('auth').hidden, false, 'rejected saved ' + mode + ' credential returns to authentication');
    assert.equal(rejected.get('workspace').hidden, true);
    assert.equal(rejected.sessionStorage.has(sessionKey), false, 'rejected saved ' + mode + ' credential is removed');
    assert.equal(rejected.timers.size, 0);
    assertPrivate(rejected, tokens.viewer);
    const reload = browser(input, request, { pathname: '/', sessionStorage: rejected.sessionStorage }); await idle(reload);
    assert.equal(reload.calls.length, 0, 'rejected credential is not retried on reload');
    assert.equal(reload.get('auth').hidden, false);
  }
}

async function runTranscript(input, request, api, tokens) {
  const texts = [
    'Transcript direct for the humans <img src=x onerror=alert(1)>',
    'Transcript ask for the humans',
    'Transcript answer for the humans',
    'Transcript team post for the humans',
    'Transcript huddle idea for the humans',
    'Transcript huddle comment <script>bad()</script> ' + 'Complete text. '.repeat(50).trimEnd(),
  ];
  const direct = await request('/v1/messages', {
    method: 'POST', headers: { Authorization: 'Bearer ' + tokens.a, 'Content-Type': 'application/json' },
    body: JSON.stringify({ to: 'agent-b', text: texts[0] }),
  });
  assert.equal(direct.status, 201);
  await flush();
  const ask = await api('/api/ask', { to: 'agent-b', question: texts[1], wait_s: 0 });
  await flush();
  await api('/api/reply', { ask_id: ask.ask_id, answer: texts[2] }, tokens.b);
  await flush();
  await api('/api/post', { kind: 'update', text: texts[3] });
  const talk = await api('/api/huddle/start', { kind: 'brainstorm', topic: 'Transcript regression huddle' });
  await flush();
  await api('/api/huddle/post', { id: talk.huddle.id, kind: 'idea', text: texts[4] });
  await flush();
  await api('/api/huddle/post', { id: talk.huddle.id, kind: 'comment', text: texts[5] }, tokens.b);
  for (const mode of ['member', 'agent']) {
    const app = browser(input, request, mode === 'member' ? { hash: '#k=' + encodeURIComponent(tokens.a) } : { pathname: '/' });
    if (mode === 'agent') await login(app, tokens.a); else await idle(app);
    const panel = app.get('messages'), cards = panel.querySelectorAll('article');
    const source = app.calls.find(call => call.url.startsWith('/api/legacy/transcript?') && call.status === 200).payload.messages;
    const entries = texts.map(text => cards.find(card => card.textContent.includes(text)));
    assert.ok(entries.every(Boolean), mode + ' transcript renders every full message text');
    assert.deepEqual(entries.map(card => cards.indexOf(card)), entries.map(card => cards.indexOf(card)).sort((a, b) => a - b), mode + ' transcript is chronological');
    for (const [index, kind] of ['direct', 'ask', 'answer', 'huddle post', 'huddle post', 'huddle comment'].entries()) {
      assert.ok(entries[index].textContent.includes(kind + ' · '), mode + ' shows message kind ' + kind);
      assert.ok(entries[index].textContent.includes(new Date(source.find(row => row.text === texts[index]).created_at).toLocaleString()), mode + ' shows message time');
      assert.ok(entries[index].textContent.includes(index === 2 || index === 5 ? 'agent-b' : 'agent-a'), mode + ' shows sender');
      assert.ok(entries[index].textContent.includes('→ ' + (index < 2 ? 'agent-b' : index === 2 ? 'agent-a' : 'team')), mode + ' shows recipient');
      if (index) assert.ok(!entries[index].textContent.includes('Unacknowledged'), 'only direct messages have acknowledgement state');
    }
    for (const index of [1, 2]) assert.ok(entries[index].textContent.includes(ask.ask_id), 'question id is visible');
    for (const index of [4, 5]) assert.ok(entries[index].textContent.includes(talk.huddle.id), 'huddle id is visible');
    assert.equal(panel.querySelectorAll('img').length, 0, 'message markup stays literal text');
    assert.equal(panel.querySelectorAll('script').length, 0, 'huddle markup stays literal text');
    assert.equal(Number(app.get('message-count').textContent), cards.length);
    await app.get('sign-out').click(); await idle(app);
    assert.equal(panel.textContent, '', 'sign-out clears the merged transcript');
  }
}

async function runActions(input, request, api, tokens) {
  const tasks = ['take', 'accept', 'decline', 'start', 'release', 'done'];
  const planned = await api('/api/plan', { tasks: tasks.map(action => ({
    key: action.toUpperCase(), title: 'Unified ' + action,
    ...(action === 'take' ? {} : { assignee: 'agent-a' }),
  })) });
  assert.equal(planned.tasks.length, tasks.length);
  for (const key of ['start', 'release', 'done']) await api('/api/task', { key, action: 'accept' });
  await api('/api/task', { key: 'done', action: 'start' });
  const talk = await api('/api/huddle/start', { kind: 'brainstorm', topic: 'Unified huddle', context: 'Keep all features in one viewer' });
  const idea = await api('/api/huddle/post', { id: talk.huddle.id, kind: 'idea', text: 'A peer idea to vote on' }, tokens.b);
  const question = await api('/api/ask', { to: 'agent-a', question: 'A peer question for the viewer', wait_s: 0 }, tokens.b);
  const app = browser(input, request); await login(app, tokens.a);
  assert.match(app.get('house-rules').textContent, /lead|task|master prompt|file/i, 'team house rules are rendered separately from the coordination guide');
  for (const [action, label, expected] of [
    ['take', 'Take it', 'assigned'], ['accept', 'Accept', 'assigned'], ['decline', 'Decline', 'open'],
    ['start', 'Start', 'in_progress'], ['release', 'Release', 'open'], ['done', 'Mark done', 'done'],
  ]) {
    const card = app.get('tasks').querySelectorAll('article').find(node => node.textContent.includes('Unified ' + action));
    assert.ok(card, 'task card for ' + action);
    const body = postFrom(await click(app, findButton(card, label)), '/api/task');
    assert.equal(body.action, action); assert.equal(body.key, action.toUpperCase());
    assert.equal((await api('/api/state')).tasks.find(task => task.key === body.key).status, expected);
  }

  const huddle = () => app.get('huddle-list').querySelectorAll('article').find(node => node.textContent.includes('Unified huddle'));
  for (const [label, vote] of [['Back it', 'up'], ['Push back', 'down']]) {
    const body = postFrom(await click(app, findButton(huddle(), label)), '/api/huddle/vote');
    assert.equal(body.id, talk.huddle.id); assert.equal(body.idea, idea.seq); assert.equal(body.vote, vote);
  }
  const ideaInput = huddle().querySelectorAll('input').find(node => node.getAttribute('placeholder') === 'Add an idea');
  assert.ok(ideaInput, 'huddle has the upstream add-idea control'); ideaInput.value = 'Viewer-authored idea'; await ideaInput.fire('input');
  const added = postFrom(await click(app, findButton(huddle(), 'Add idea')), '/api/huddle/post');
  assert.equal(added.text, 'Viewer-authored idea'); assert.equal(added.kind, 'idea');
  const showTalk = huddle().querySelectorAll('button').find(node => /^Show discussion/.test(node.textContent));
  assert.ok(showTalk, 'huddle discussion can be opened'); await click(app, showTalk);
  assert.ok(huddle().textContent.includes('A peer idea to vote on'), 'loaded discussion contains hub posts');
  assert.ok(app.calls.some(call => call.url.startsWith('/api/huddle?id=')), 'discussion reads the hub detail endpoint');
  await set(app, 'hub-huddle-kind', 'design'); await set(app, 'hub-huddle-topic', 'A huddle started in the viewer');
  await set(app, 'hub-huddle-proposal', 'Retain the viewer layout and reuse the team hub behavior');
  await set(app, 'hub-huddle-context', 'A design huddle must carry its proposal');
  let before = app.calls.length; await app.get('hub-huddle-form').fire('submit'); await idle(app);
  const started = postFrom(app.calls.slice(before), '/api/huddle/start');
  assert.equal(started.kind, 'design'); assert.equal(started.topic, 'A huddle started in the viewer');
  assert.equal(started.proposal, 'Retain the viewer layout and reuse the team hub behavior');
  assert.equal(started.context, 'A design huddle must carry its proposal');
  const startedId = (await api('/api/state')).huddles.find(huddle => huddle.topic === started.topic).id;
  await set(app, 'hub-huddle-decision-' + startedId, 'Keep the viewer as the only dashboard');
  before = app.calls.length; await app.get('hub-huddle-close-' + startedId).fire('submit'); await idle(app);
  const closed = postFrom(app.calls.slice(before), '/api/huddle/close'); assert.equal(closed.id, startedId);
  assert.equal((await api('/api/state')).huddles.find(huddle => huddle.id === startedId).status, 'closed');

  // Use the original prompt section/editor semantics, including rejection,
  // approving, locking, and amendment while the locked version remains in force.
  const section = app.get('master').querySelectorAll('article').find(node => node.querySelectorAll('button').some(button => button.textContent === 'Edit'));
  assert.ok(section, 'master prompt exposes editable sections');
  await click(app, findButton(section, 'Edit'));
  let editor = app.get('master').querySelector('textarea'); assert.ok(editor); editor.value = 'A unified, team-scoped dashboard'; await editor.fire('input');
  const editBody = postFrom(await click(app, findButton(app.get('master'), 'Save section')), '/api/prompt/edit');
  assert.equal(editBody.text, 'A unified, team-scoped dashboard'); assert.equal(editBody.mode, 'replace');
  postFrom(await click(app, findButton(app.get('master'), 'Submit for vote')), '/api/prompt/submit');
  let comment = app.get('master').querySelectorAll('input').find(node => node.getAttribute('aria-label') === 'Comment on your vote');
  assert.ok(comment); comment.value = 'Keep this comment through a live snapshot'; comment.focus(); comment.setSelectionRange(5, 12); await comment.fire('input');
  await poll(app);
  comment = app.get('master').querySelectorAll('input').find(node => node.getAttribute('aria-label') === 'Comment on your vote');
  assert.equal(comment.value, 'Keep this comment through a live snapshot', 'refresh preserves a pending vote comment');
  assert.equal(app.document.activeElement, comment, 'refresh preserves vote comment keyboard focus');
  assert.equal(comment.selectionStart, 5); assert.equal(comment.selectionEnd, 12);
  const reject = postFrom(await click(app, findButton(app.get('master'), 'Reject')), '/api/prompt/vote'); assert.equal(reject.approve, false);
  assert.equal(reject.comment, 'Keep this comment through a live snapshot');
  const approve = postFrom(await click(app, findButton(app.get('master'), 'Approve')), '/api/prompt/vote'); assert.equal(approve.approve, true);
  await api('/api/prompt/vote', { approve: true }, tokens.b);
  await poll(app);
  assert.match(app.get('prompt-badge').textContent, /locked/i, 'all approvals lock the master prompt');
  const locked = (await api('/api/state')).prompt.locked_version;
  const firstEdit = app.get('master').querySelectorAll('button').find(button => button.textContent === 'Edit');
  await click(app, firstEdit); editor = app.get('master').querySelector('textarea'); editor.value = 'An amended unified dashboard'; await editor.fire('input');
  postFrom(await click(app, findButton(app.get('master'), 'Save section')), '/api/prompt/edit');
  const amended = (await api('/api/state')).prompt; assert.equal(amended.state, 'amending'); assert.equal(amended.locked_version, locked);
  assert.match(app.get('prompt-badge').textContent, /amending/i);

  await set(app, 'hub-question-to', (await api('/api/state')).members.find(member => member.name === 'agent-b').id);
  await set(app, 'hub-question-text', 'Does the unified question form work?');
  before = app.calls.length; await app.get('hub-question-form').fire('submit'); await idle(app);
  const ask = postFrom(app.calls.slice(before), '/api/ask'); assert.equal(ask.question, 'Does the unified question form work?'); assert.equal(ask.wait_s, 0, 'UI asks do not hold a request open');
  const questionCard = app.get('questions').querySelectorAll('article').find(node => node.textContent.includes('A peer question for the viewer'));
  assert.ok(questionCard, 'incoming questions appear with answer controls');
  const answerInput = questionCard.querySelector('textarea') || questionCard.querySelector('input');
  assert.ok(answerInput); answerInput.value = 'The viewer can answer this'; await answerInput.fire('input');
  answerInput.focus(); answerInput.setSelectionRange(4, 8); await poll(app);
  const refreshedAnswer = app.get('hub-answer-' + question.ask_id);
  assert.equal(refreshedAnswer.value, 'The viewer can answer this', 'a pending answer survives live refresh');
  assert.equal(app.document.activeElement, refreshedAnswer, 'refresh preserves answer keyboard focus');
  assert.equal(refreshedAnswer.selectionStart, 4); assert.equal(refreshedAnswer.selectionEnd, 8);
  before = app.calls.length; await app.get('hub-answer-form-' + question.ask_id).fire('submit'); await idle(app);
  const reply = postFrom(app.calls.slice(before), '/api/reply'); assert.equal(reply.ask_id, question.ask_id); assert.equal(reply.answer, 'The viewer can answer this');
  assert.ok(!(await api('/api/state')).asks_for_me.some(ask => ask.id === question.ask_id), 'answered questions leave the pending asks');

  await set(app, 'hub-claim-path', 'src/unified-viewer-test.txt');
  before = app.calls.length; await app.get('hub-claim-form').fire('submit'); await idle(app);
  const claim = postFrom(app.calls.slice(before), '/api/claims'); assert.deepEqual(claim.paths, ['src/unified-viewer-test.txt']);
  assert.ok(app.get('claims').textContent.includes('src/unified-viewer-test.txt'));
  const claimed = app.get('claims').querySelectorAll('article').find(node => node.textContent.includes('src/unified-viewer-test.txt'));
  const release = postFrom(await click(app, findButton(claimed, 'Release')), '/api/claims');
  assert.equal(release.action, 'release'); assert.deepEqual(release.paths, ['src/unified-viewer-test.txt']);
  assert.ok(!(await api('/api/state')).claims.some(claim => claim.path === 'src/unified-viewer-test.txt'));
  assert.ok(app.get('news').textContent.includes('Viewer-authored idea') || app.get('news').textContent.includes('master prompt'), 'hub actions appear in the live feed');

  // The small live-feed window is not the question store. Unrelated events must
  // not erase an outgoing open ask (which is absent from asks_for_me) or its reply.
  const observedAsk = app.calls.find(call => call.url === '/api/ask' && call.payload?.ask_id);
  assert.ok(observedAsk, 'the outgoing ask has a real hub-issued ID');
  const cursor = (await api('/api/state')).last_event_id + 10000;
  for (let i = 0; i < 155; i++) app.sendChunk('data: ' + JSON.stringify({
    id: cursor + i, kind: 'update', member_id: null, data: { text: 'Unrelated update ' + i }, created_at: Date.now(),
  }) + '\n\n');
  await idle(app);
  assert.ok(app.get('questions').textContent.includes('Does the unified question form work?'), 'outgoing open question survives feed eviction');
  assert.ok(app.get('questions').textContent.includes('The viewer can answer this'), 'observed answer survives feed eviction');
  assert.ok(app.get('news').children.length <= 150, 'the live feed remains bounded');
  const answer = 'The older outgoing question still accepts replies';
  await api('/api/reply', { ask_id: observedAsk.payload.ask_id, answer }, tokens.b);
  await poll(app);
  assert.ok(app.get('questions').textContent.includes(answer), 'a later answer attaches to the retained outgoing question');

}

async function runWorker() {
  const { load, context } = require('./runtime.cjs');
  const worker = load(path.join(root, 'cloudflare/src/index.ts'));
  const legacy = context(), database = context();
  const env = { AGENT_A_TOKEN: 'a'.repeat(40), AGENT_B_TOKEN: 'b'.repeat(40), VIEWER_TOKEN: 'v'.repeat(40) };
  const room = new worker.Room(legacy.ctx, env), hub = new worker.HubRoom(database.ctx, env);
  env.ROOM = { idFromName: name => name, get: () => ({ fetch: req => room.fetch(req) }) };
  env.HUB = { idFromName: name => name, get: () => ({
    fetch: req => hub.fetch(req), teamTranscript: teamId => hub.teamTranscript(teamId), legacyTeamId: () => hub.legacyTeamId(),
  }) };
  try {
    await run((route, options) => worker.default.fetch(new Request('https://agentmesh.zahid.win' + route, options), env),
      { a: env.AGENT_A_TOKEN, b: env.AGENT_B_TOKEN, viewer: env.VIEWER_TOKEN });
  } finally { legacy.db.close(); database.db.close(); }
}

module.exports = { browser, parseDocument, idle, login, poll, findButton, click, set, postFrom, run, runWorker };

if (require.main === module) (async () => {
  const payload = process.argv[2] ? JSON.parse(process.argv[2]) : null;
  if (payload) await run((route, options) => fetch(payload.origin + route, options), payload.tokens);
  else await runWorker();
})().catch(error => { console.error(error); process.exitCode = 1; });
