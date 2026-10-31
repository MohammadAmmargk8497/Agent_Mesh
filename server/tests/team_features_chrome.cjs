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
    } else if (message.method === 'Fetch.requestPaused') {
      const { requestId, request } = message.params;
      const url = new URL(request.url);
      const local = url.hostname === '127.0.0.1' || ['about:', 'data:'].includes(url.protocol);
      // Browser subresources (such as optional web fonts) cannot reach external hosts.
      await call(local ? 'Fetch.continueRequest' : 'Fetch.failRequest', local ? { requestId } : {
        requestId, errorReason: 'BlockedByClient',
      }).catch(() => {});
    }
  });
  return {
    call,
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
  assert.equal(new URL(settings.baseUrl).hostname, '127.0.0.1', 'Home server must stay on loopback');
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
  try {
    await cdp.call('Page.enable');
    await cdp.call('Runtime.enable');
    await cdp.call('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
    await cdp.call('Page.navigate', { url: `${settings.baseUrl}/t/${settings.teamId}#k=${encodeURIComponent(settings.token)}` });
    await waitFor(`Boolean(document.querySelector(${literal(noteSelector)}) && document.querySelector(${literal(handoffSelector)}))`, 'saved note and task handoff');
    assert.equal(await cdp.evaluate(`document.querySelector(${literal(noteSelector)}).querySelector('h3').textContent`), settings.title);
    assert.equal(await cdp.evaluate(`document.querySelector(${literal(noteSelector)}).querySelector('.description').textContent`), settings.body);
    assert.equal(await cdp.evaluate(`document.querySelector(${literal(handoffSelector)}).querySelector('.description').textContent`), settings.summary);
    assert.equal(await cdp.evaluate(`Boolean(document.querySelector('#e2e-injected-note, #e2e-injected-handoff') || window.__teamNotesExecuted)`), false);
    assert.equal(await cdp.evaluate('location.hash'), '');
    assert.equal(await cdp.evaluate(`document.documentElement.outerHTML.includes(${literal(settings.token)})`), false);

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
    console.log('Real Chrome passed: persisted task card + Notes panel, literal text, no token in HTML, filters, create forms and confirmed hard deletion');
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
