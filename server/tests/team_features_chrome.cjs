// Dependency-free real-browser assertions; settings/credentials arrive only over stdin.
const assert = require('node:assert/strict');

async function readSettings() {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  return JSON.parse(input);
}

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function connect(url) {
  assert.equal(new URL(url).hostname, '127.0.0.1', 'CDP must stay on loopback');
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', () => reject(new Error('Cannot connect to local Chrome')), { once: true });
  });
  const pending = new Map();
  const requests = [];
  let sequence = 0;
  function call(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 15000);
      pending.set(id, { resolve, reject, timer });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }
  socket.addEventListener('message', async event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id);
      if (!request) return;
      pending.delete(message.id);
      clearTimeout(request.timer);
      if (message.error) request.reject(new Error('CDP command failed'));
      else request.resolve(message.result);
    } else if (message.method === 'Network.requestWillBeSent') {
      const { loaderId, request } = message.params;
      requests.push({ loaderId, pathname: new URL(request.url).pathname,
        method: request.method, authenticated: Object.keys(request.headers).some(name => name.toLowerCase() === 'authorization') });
    } else if (message.method === 'Fetch.requestPaused') {
      const { requestId, request } = message.params;
      const url = new URL(request.url);
      const local = ['127.0.0.1', 'localhost'].includes(url.hostname) || ['about:', 'data:'].includes(url.protocol);
      // Browser subresources (such as optional web fonts) cannot reach external hosts.
      await call(local ? 'Fetch.continueRequest' : 'Fetch.failRequest', local ? { requestId } : {
        requestId, errorReason: 'BlockedByClient',
      }).catch(() => {});
    }
  });
  return {
    call, requests,
    async evaluate(expression) {
      const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error('Browser expression failed');
      return result.result.value;
    },
    close() {
      for (const request of pending.values()) { clearTimeout(request.timer); request.reject(new Error('CDP closed')); }
      pending.clear();
      socket.close();
    },
  };
}

async function main(settings) {
  const base = new URL(settings.baseUrl);
  assert.ok(['127.0.0.1', 'localhost'].includes(base.hostname), 'Home server must stay on loopback');
  // Chromium accepts Secure cookies on the trustworthy localhost loopback origin.
  base.hostname = 'localhost'; settings.baseUrl = base.origin;
  const pages = await (await fetch(`http://127.0.0.1:${settings.debugPort}/json/list`)).json();
  const page = pages.find(target => target.type === 'page');
  assert.ok(page, 'Chrome did not create a page');
  const cdp = await connect(page.webSocketDebuggerUrl);
  const literal = value => JSON.stringify(value);
  const noteSelector = `.team-note[data-note-id="${settings.noteId}"]`;
  const handoffSelector = `.task-handoff[data-handoff-id="${settings.handoffId}"]`;
  async function waitFor(expression, label) {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (await cdp.evaluate(expression)) { await delay(200); return; }
      await delay(100);
    }
    throw new Error('Real page did not reach expected state: ' + label);
  }
  async function submit(formId, values) {
    await cdp.evaluate(`(() => {
      const values = ${literal(values)};
      for (const [id, value] of Object.entries(values)) {
        const field = document.getElementById(id);
        if (!field) throw new Error('Missing form field');
        field.value = value; field.dispatchEvent(new Event('input', { bubbles: true }));
      }
      document.getElementById(${literal(formId)}).requestSubmit();
      return true;
    })()`);
  }
  async function clickNoteButton(selector, text) {
    assert.equal(await cdp.evaluate(`(() => {
      const button = [...document.querySelector(${literal(selector)}).querySelectorAll('button')]
        .find(node => node.textContent === ${literal(text)});
      if (!button) return false; button.click(); return true;
    })()`), true, 'Missing note action');
  }
  async function checkCredentialPrivacy(label) {
    assert.equal(await cdp.evaluate(`document.documentElement.outerHTML.includes(${literal(settings.token)})`), false, label + ' has no token in HTML');
    assert.equal(await cdp.evaluate(`Array.from(document.querySelectorAll('input,textarea'), field => field.value).some(value => value.includes(${literal(settings.token)}))`), false, label + ' clears credential fields');
    assert.equal(await cdp.evaluate(`Object.entries(localStorage).some(([key, value]) => key.includes(${literal(settings.token)}) || value.includes(${literal(settings.token)}))`), false, label + ' has no token in localStorage');
    assert.equal(await cdp.evaluate(`JSON.stringify({session:Object.entries(sessionStorage),writes:window.__storageWrites}).includes(${literal(settings.token)})`), false, label + ' never writes the credential to browser storage, including transient writes');
    assert.equal(await cdp.evaluate(`document.cookie.includes('agentmesh_session') || document.cookie.includes(${literal(settings.token)})`), false, label + ' keeps the HttpOnly cookie inaccessible to scripts');
    assert.equal(await cdp.evaluate(`location.href.includes(${literal(settings.token)})`), false, label + ' strips the key from the URL');
    assert.equal(cdp.requests.some(request => /^\/(?:api|v1)\//.test(request.pathname) && request.authenticated), false, label + ' sends API requests with cookies, never client Bearer headers');
  }
  async function checkLocked(label) {
    await waitFor(`document.getElementById('auth')?.hidden === false && document.getElementById('workspace')?.hidden === true`, label + ' access form');
    const { frameTree } = await cdp.call('Page.getFrameTree');
    const loaderId = frameTree.frame.loaderId;
    assert.ok(loaderId, label + ' has a current document loader');
    assert.equal(cdp.requests.some(request => request.loaderId === loaderId && (request.authenticated || (/^\/(?:api|v1)\//.test(request.pathname) && (request.pathname !== '/api/state' || request.method !== 'GET')))), false, label + ' makes only the read-only anonymous session bootstrap request');
    assert.ok(cdp.requests.some(request => request.loaderId === loaderId && request.pathname === '/api/state' && request.method === 'GET'), label + ' checks its cookie session on load');
    assert.equal((await cdp.call('Network.getCookies', { urls: [settings.baseUrl] })).cookies.some(cookie => cookie.name === 'agentmesh_session'), false, label + ' has no session cookie');
    assert.equal(await cdp.evaluate(`sessionStorage.getItem('agentmesh:session')`), null, label + ' has no remembered session');
    await checkCredentialPrivacy(label);
  }
  async function checkBoard(label) {
    const count = `${settings.doneCount} of ${settings.tasks.length} done`;
    await waitFor(`document.getElementById('task-count')?.textContent === ${literal(count)}`, label + ' full team count');
    assert.equal(await cdp.evaluate('location.pathname'), '/', 'one page stays at root');
    const cards = await cdp.evaluate(`Array.from(document.querySelectorAll('#tasks .task-card'), card => ({
      key: card.querySelector('.key').textContent,
      text: card.textContent,
      column: card.closest('.board-column').querySelector('h3').firstChild.textContent.trim(),
    }))`);
    assert.deepEqual(cards.map(card => card.key).sort(), settings.tasks.map(task => task.key).sort(), label + ' every task key');
    for (const task of settings.tasks) {
      const card = cards.find(card => card.key === task.key);
      assert.ok(card.text.includes(task.title), label + ' task title ' + task.key);
      assert.match(card.column, { open: /Proposed/, proposed: /Proposed/, assigned: /Assigned/,
        in_progress: /In progress/, done: /Done/ }[task.status], label + ' status ' + task.key);
      if (task.assignee_id || task.proposed_id) assert.ok(card.text.includes('agent-b'), label + ' assignee ' + task.key);
    }
    assert.ok(cards.some(card => card.key === 'T55' && card.text.includes('Team task 55')), label + ' newest task');
    await waitFor(`document.getElementById('messages').textContent.includes('One-page transcript remains visible')`, label + ' transcript');
    const cookie = (await cdp.call('Network.getCookies', { urls: [settings.baseUrl] })).cookies.find(cookie => cookie.name === 'agentmesh_session');
    assert.ok(cookie, label + ' keeps the browser signed in with a cookie');
    assert.equal(cookie.value, settings.token);
    assert.equal(cookie.httpOnly, true, label + ' cookie is HttpOnly');
    assert.equal(cookie.secure, true, label + ' cookie is Secure');
    assert.equal(cookie.sameSite, 'Strict', label + ' cookie is SameSite=Strict');
    assert.equal(cookie.path, '/');
    assert.equal(cookie.session, false, label + ' cookie persists after closing a tab');
    assert.ok(cookie.expires > Date.now() / 1000 + 2591800 && cookie.expires <= Date.now() / 1000 + 2592001, label + ' cookie expires after 30 days');
    await checkCredentialPrivacy(label);
  }
  try {
    await cdp.call('Page.enable');
    await cdp.call('Runtime.enable');
    await cdp.call('Network.enable');
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
      window.__storageWrites = [];
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        window.__storageWrites.push({ storage: this === localStorage ? 'localStorage' : 'sessionStorage', key: String(key), value: String(value) });
        return original.call(this, key, value);
      };
    })();` });
    await cdp.call('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
    await cdp.call('Page.navigate', { url: settings.baseUrl + '/' });
    await waitFor(`Boolean(document.getElementById('access-form'))`, 'root access form');
    await submit('access-form', { 'access-key': settings.token });
    await checkBoard('typed member key');
    assert.equal(await cdp.evaluate(`Boolean(document.getElementById('auto-respond'))`), true, 'legacy agent settings retained');
    cdp.requests.length = 0;
    await cdp.call('Page.reload');
    await checkBoard('reload with persistent HttpOnly cookie');
    await cdp.evaluate(`(() => {
      sessionStorage.setItem('agentmesh:session', JSON.stringify({ key: ${literal(settings.token)}, mode: 'agent' }));
      for (const key of ['agentmesh-agent', 'agentmesh-viewer']) localStorage.setItem(key, ${literal(settings.token)});
      localStorage.setItem('agentmesh-access-mode', 'agent');
    })()`);
    cdp.requests.length = 0;
    await cdp.call('Page.reload');
    await checkBoard('valid cookie session with obsolete credential storage cleanup');
    assert.equal(await cdp.evaluate(`sessionStorage.getItem('agentmesh:session')`), null, 'obsolete tab credential removed');
    for (const key of ['agentmesh-agent', 'agentmesh-viewer', 'agentmesh-access-mode']) {
      assert.equal(await cdp.evaluate(`localStorage.getItem(${literal(key)})`), null, 'obsolete credential preference removed: ' + key);
    }
    await cdp.evaluate(`document.getElementById('sign-out').click()`);
    await waitFor(`document.getElementById('auth')?.hidden===false && document.getElementById('workspace')?.hidden===true`, 'explicit sign-out');
    assert.equal((await cdp.call('Network.getCookies', { urls: [settings.baseUrl] })).cookies.some(cookie => cookie.name === 'agentmesh_session'), false, 'sign-out clears the persistent cookie');
    await checkCredentialPrivacy('explicit sign-out');
    cdp.requests.length = 0;
    await cdp.call('Page.reload');
    await checkLocked('signed-out reload');
    await cdp.call('Page.navigate', { url: 'about:blank' });
    await cdp.call('Page.navigate', { url: `${settings.baseUrl}/#k=${encodeURIComponent(settings.token)}` });
    await waitFor(`Boolean(document.querySelector(${literal(noteSelector)}) && document.querySelector(${literal(handoffSelector)}))`, 'saved note and task handoff');
    await checkBoard('fragment member key');
    for (const id of ['messages', 'huddles', 'master', 'questions', 'claims', 'team-notes-panel', 'members', 'patches', 'audit', 'news', 'member-heartbeats']) {
      assert.equal(await cdp.evaluate(`document.getElementById(${literal(id)}).getClientRects().length > 0`), true, 'panel retained: ' + id);
    }
    await cdp.evaluate(`(() => { if (document.documentElement.getAttribute('data-theme') !== 'dark') document.getElementById('theme-toggle').click(); })()`);
    assert.equal(await cdp.evaluate(`document.documentElement.getAttribute('data-theme')`), 'dark');
    assert.equal(await cdp.evaluate(`document.querySelector(${literal(noteSelector)}).querySelector('h3').textContent`), settings.title);
    assert.equal(await cdp.evaluate(`document.querySelector(${literal(noteSelector)}).querySelector('.description').textContent`), settings.body);
    assert.equal(await cdp.evaluate(`document.querySelector(${literal(handoffSelector)}).querySelector('.description').textContent`), settings.summary);
    assert.equal(await cdp.evaluate(`Boolean(document.querySelector('#e2e-injected-note, #e2e-injected-handoff') || window.__teamNotesExecuted)`), false);
    assert.equal(await cdp.evaluate('location.hash'), '');
    await checkCredentialPrivacy('fragment authentication');

    await submit('home-notes-filter', { 'home-notes-query': 'Local note', 'home-notes-tag': 'e2e' });
    await waitFor(`document.querySelectorAll('#home-notes-list .team-note').length === 1 && Boolean(document.querySelector(${literal(noteSelector)}))`, 'combined search and tag filter');
    await submit('home-notes-filter', { 'home-notes-query': 'no such note', 'home-notes-tag': 'e2e' });
    await waitFor(`document.querySelectorAll('#home-notes-list .team-note').length === 0`, 'empty search results');
    await cdp.evaluate(`[...document.querySelectorAll('#home-notes-filter button')].find(node => node.textContent === 'Clear filters').click()`);
    await waitFor(`document.querySelectorAll('#home-notes-list .team-note').length === 2`, 'clear filters');

    await submit('home-note-form', { 'home-note-title': 'Browser-created note',
      'home-note-body': 'Saved from real Chrome', 'home-note-tags': 'browser, e2e' });
    const browserNote = `.team-note[data-note-id]`;
    await waitFor(`[...document.querySelectorAll(${literal(browserNote)})].some(node => node.querySelector('h3').textContent === 'Browser-created note')`, 'browser note creation');
    const browserNoteId = await cdp.evaluate(`[...document.querySelectorAll(${literal(browserNote)})].find(node => node.querySelector('h3').textContent === 'Browser-created note').dataset.noteId`);
    const createdSelector = `.team-note[data-note-id="${browserNoteId}"]`;
    await clickNoteButton(createdSelector, 'Delete note');
    await waitFor(`Boolean(document.querySelector(${literal(createdSelector)})?.querySelector('[data-note-delete]'))`, 'explicit delete confirmation');
    await clickNoteButton(createdSelector, 'Cancel deletion');
    assert.equal(await cdp.evaluate(`Boolean(document.querySelector(${literal(createdSelector)}))`), true);
    await clickNoteButton(createdSelector, 'Delete note');
    await clickNoteButton(createdSelector, 'Confirm delete');
    await waitFor(`!document.querySelector(${literal(createdSelector)})`, 'confirmed note deletion');

    const prefix = `home-handoff-${settings.taskKey}-`;
    await submit(prefix + 'form', { [prefix + 'summary']: 'Browser-created handoff',
      [prefix + 'files']: 'server/tests/team_features_e2e.py', [prefix + 'questions']: 'No open questions',
      [prefix + 'next']: 'Review' });
    await waitFor(`document.querySelectorAll('.task-handoffs .task-handoff').length === 2 && document.querySelector('.task-handoffs .task-handoff .description').textContent === 'Browser-created handoff'`, 'newest-first browser handoff creation');
    cdp.requests.length = 0;
    await cdp.call('Page.navigate', { url: settings.baseUrl + '/viewer' });
    await checkBoard('viewer alias restores the persistent cookie session');
    assert.equal(await cdp.evaluate(`document.documentElement.getAttribute('data-theme')`), 'dark', 'theme retained after reload');
    console.log('Real Chrome passed: 38 of 55 team tasks on one root page, newest task, statuses, assignees, typed/fragment cookie authentication and automatic reload restoration, stale browser storage credentials purged, viewer alias, other panels, literal text, no token in HTML or browser storage, persistent HttpOnly session cookie, filters, create forms and confirmed hard deletion');
  } finally {
    cdp.close();
  }
}

let settings;
readSettings().then(value => { settings = value; return main(value); }).catch(error => {
  const message = String(error.message || error);
  console.error(settings?.token ? message.replaceAll(settings.token, '[redacted]') : message);
  process.exitCode = 1;
});
