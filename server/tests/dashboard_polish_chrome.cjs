// Real headless Chrome/CDP; fixture settings arrive over stdin, never arguments.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function connect(url, origin) {
  assert.equal(new URL(url).hostname, '127.0.0.1');
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', () => reject(new Error('Cannot connect to loopback Chrome')), { once: true });
  });
  let sequence = 0;
  const pending = new Map(), remoteRequests = [], exceptions = [], requests = [], consoleErrors = [];
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
      pending.delete(message.id); clearTimeout(request.timer);
      if (message.error) request.reject(new Error('CDP command failed: ' + message.error.message));
      else request.resolve(message.result);
    } else if (message.method === 'Network.requestWillBeSent') {
      const { request } = message.params;
      requests.push({ path: new URL(request.url).pathname, method: request.method,
        authorization: Object.keys(request.headers).some(name => name.toLowerCase() === 'authorization') });
    } else if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
      consoleErrors.push(message.params.args.map(argument => argument.value ?? argument.description ?? '').join(' '));
    } else if (message.method === 'Runtime.exceptionThrown') {
      exceptions.push(message.params.exceptionDetails.text);
    } else if (message.method === 'Fetch.requestPaused') {
      const { requestId, request } = message.params;
      const url = new URL(request.url);
      const local = url.origin === origin || ['about:', 'data:', 'blob:'].includes(url.protocol);
      if (!local) remoteRequests.push(url.origin);
      await call(local ? 'Fetch.continueRequest' : 'Fetch.failRequest', local ? { requestId } : { requestId, errorReason: 'BlockedByClient' }).catch(() => {});
    }
  });
  return {
    call, remoteRequests, exceptions, requests, consoleErrors,
    async evaluate(expression) {
      const result = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
      if (result.exceptionDetails) throw new Error('Browser evaluation failed: ' + (result.exceptionDetails.exception?.description || result.exceptionDetails.text));
      return result.result.value;
    },
    close() {
      for (const request of pending.values()) { clearTimeout(request.timer); request.reject(new Error('CDP closed')); }
      pending.clear(); socket.close();
    },
  };
}

async function main(settings) {
  const origin = new URL(settings.baseUrl);
  assert.equal(origin.hostname, 'localhost', 'Only the local Secure-cookie fixture is allowed');
  const pages = await (await fetch(`http://127.0.0.1:${settings.debugPort}/json/list`)).json();
  const initialPage = pages.find(page => page.type === 'page');
  let cdp = await connect(initialPage.webSocketDebuggerUrl, origin.origin);
  const version = await (await fetch(`http://127.0.0.1:${settings.debugPort}/json/version`)).json();
  const browser = await connect(version.webSocketDebuggerUrl, origin.origin);
  let currentTarget = initialPage.id;
  const sessions = [cdp];
  const evidence = { checks: [], layouts: [], screenshots: [], exports: [] };
  fs.mkdirSync(settings.evidence, { recursive: true });
  async function screenshot(name) {
    const { data } = await cdp.call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(settings.evidence, name + '.png'), Buffer.from(data, 'base64'));
    evidence.screenshots.push(name + '.png');
  }
  const literal = JSON.stringify;
  async function waitFor(expression, label) {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (await cdp.evaluate(expression)) return;
      await delay(60);
    }
    const state = await cdp.evaluate(`({authHidden:document.getElementById('auth')?.hidden,authError:document.getElementById('auth-error')?.textContent,status:document.getElementById('connection-status')?.textContent,cards:document.querySelectorAll('#tasks .task-card').length,messages:document.getElementById('messages')?.textContent,exceptions:${literal(cdp.exceptions)}})`);
    throw new Error('Dashboard did not reach expected state: ' + label + ' ' + JSON.stringify(state));
  }
  async function click(id) {
    await cdp.evaluate(`document.getElementById(${literal(id)}).click()`);
  }
  async function set(id, value, event = 'input') {
    await cdp.evaluate(`(() => { const field = document.getElementById(${literal(id)}); field.value = ${literal(value)}; field.dispatchEvent(new Event(${literal(event)}, {bubbles:true})); })()`);
  }
  async function key(key, code = 'Key' + key.toUpperCase(), modifiers = 0) {
    await cdp.call('Input.dispatchKeyEvent', { type: 'keyDown', key, code, modifiers });
    await cdp.call('Input.dispatchKeyEvent', { type: 'keyUp', key, code, modifiers });
  }
  async function setViewport(width) {
    await cdp.call('Emulation.setDeviceMetricsOverride', { width, height: width <= 390 ? 1000 : 1200, deviceScaleFactor: 1, mobile: width <= 390 });
  }
  async function search(query, kind) {
    await set('global-search', query);
    await waitFor(`Boolean(document.querySelector('#search-results a[data-kind="${kind}"]'))`, kind + ' search result');
    const result = await cdp.evaluate(`(() => { const link = document.querySelector('#search-results a[data-kind="${kind}"]'); const target = document.getElementById(decodeURIComponent(link.hash.slice(1))); return {text:link.textContent, target:!!target}; })()`);
    assert.equal(result.target, true, kind + ' search links to an existing item');
    await cdp.evaluate(`document.querySelector('#search-results a[data-kind="${kind}"]').click()`);
    assert.ok(await cdp.evaluate(`document.activeElement.textContent.toLowerCase().includes(${literal(query.toLowerCase())})`), kind + ' search focuses its item');
    assert.ok(await cdp.evaluate(`document.activeElement.getClientRects().length > 0`), kind + ' search reveals its item, including on phones');
    assert.equal(await cdp.evaluate(`document.querySelectorAll('#section-nav, .nav-group, .group-count, .nav-count, [data-section]').length`), 0, kind + ' search reveals its target without restoring group navigation');
    evidence.checks.push('search: ' + kind);
  }
  async function checkCookiePrivacy(label) {
    const cookie = (await cdp.call('Network.getCookies', { urls: [origin.href] })).cookies.find(cookie => cookie.name === 'agentmesh_session');
    assert.ok(cookie, label + ' has a session cookie');
    assert.equal(cookie.value, settings.credential, label + ' cookie contains the checked access key');
    assert.equal(cookie.httpOnly, true, label + ' cookie is HttpOnly');
    assert.equal(cookie.secure, true, label + ' cookie is Secure');
    assert.equal(cookie.sameSite, 'Strict', label + ' cookie is SameSite=Strict');
    assert.equal(cookie.path, '/', label + ' cookie covers the dashboard and API');
    assert.equal(cookie.session, false, label + ' cookie survives closing a tab');
    assert.ok(cookie.expires > Date.now() / 1000 + 2591800 && cookie.expires <= Date.now() / 1000 + 2592001, label + ' cookie expires after 30 days');
    assert.equal(await cdp.evaluate(`document.cookie.includes('agentmesh_session') || document.cookie.includes(${literal(settings.credential)})`), false, label + ' script cannot read the cookie');
    assert.equal(await cdp.evaluate(`document.documentElement.outerHTML.includes(${literal(settings.credential)}) || location.href.includes(${literal(settings.credential)}) || Array.from(document.querySelectorAll('input,textarea')).some(field=>field.value.includes(${literal(settings.credential)}))`), false, label + ' clears the key from fields, HTML and URL');
    assert.equal(await cdp.evaluate(`JSON.stringify({local:Object.entries(localStorage),session:Object.entries(sessionStorage),writes:window.__storageWrites||[]}).includes(${literal(settings.credential)})`), false, label + ' never writes the key into either browser storage');
    assert.equal(cdp.requests.some(request => /^\/(?:api|v1)\//.test(request.path) && request.authorization), false, label + ' browser sends no API Bearer credentials');
  }
  async function openPage(browserContextId) {
    const { targetId } = await browser.call('Target.createTarget', { url: 'about:blank', ...(browserContextId ? { browserContextId } : {}) });
    const deadline = Date.now() + 15000;
    let target;
    while (!target) {
      target = (await (await fetch(`http://127.0.0.1:${settings.debugPort}/json/list`)).json()).find(target => target.id === targetId);
      assert.ok(Date.now() < deadline, 'Chrome creates the local test page');
      if (!target) await delay(50);
    }
    const page = await connect(target.webSocketDebuggerUrl, origin.origin);
    sessions.push(page);
    await page.call('Page.enable'); await page.call('Runtime.enable'); await page.call('Network.enable');
    await page.call('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
    await page.call('Page.addScriptToEvaluateOnNewDocument', { source: `
      window.__cspViolations = [];
      document.addEventListener('securitypolicyviolation',event=>window.__cspViolations.push(event.violatedDirective));
      window.__storageWrites = [];
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        window.__storageWrites.push({key:String(key), value:String(value)});
        return original.call(this, key, value);
      };
    ` });
    await page.call('Page.navigate', { url: origin.href });
    return { cdp: page, targetId };
  }
  const cards = `Array.from(document.querySelectorAll('#tasks .task-card')).filter(card => card.getClientRects().length).map(card => card.dataset.taskKey)`;
  async function noHorizontalScroll(width, view) {
    const size = await cdp.evaluate(`({viewport:document.documentElement.clientWidth, document:document.documentElement.scrollWidth, body:document.body.scrollWidth})`);
    assert.ok(size.document <= size.viewport && size.body <= size.viewport, `${width}px ${view}: page must not scroll horizontally (${JSON.stringify(size)})`);
  }
  const panelIds = ['members-panel', 'rules-panel', 'claims-panel', 'questions-panel',
    'board-panel', 'huddles-panel', 'plans-panel', 'transcript-panel', 'team-notes-panel',
    'handoffs-panel', 'activity-panel', 'patches-panel', 'audit-panel'];
  async function checkUngroupedLayout(width) {
    await click('board-view');
    await cdp.evaluate('window.scrollTo(0,0)');
    const layout = await cdp.evaluate(`(() => {
      const workspace=document.getElementById('workspace');
      const columns=Array.from(workspace.children).filter(node=>node.classList.contains('col'));
      const rect=node=>{const value=node.getBoundingClientRect();return {left:value.left,top:value.top+window.scrollY,width:value.width};};
      return {navigation:document.querySelectorAll('#section-nav, .section-nav, .nav-group, .nav-heading, .group-count, .nav-count, [data-section], [data-section-active]').length,
        wrapper:!!document.querySelector('.dashboard-layout, .dashboard-content'),
        headings:Array.from(document.querySelectorAll('h2,h3,h4'),node=>node.textContent.trim()).filter(text=>['Work','Conversation','Knowledge','Team and activity'].includes(text)),
        tracks:getComputedStyle(workspace).gridTemplateColumns.split(' ').map(Number.parseFloat),
        columns:columns.map(node=>({className:node.className,display:getComputedStyle(node).display,...rect(node)})),
        panels:${literal(panelIds)}.map(id=>({id,visible:!!document.getElementById(id)?.getClientRects().length})),
        inlineStyles:document.querySelectorAll('[style]').length};
    })()`);
    assert.equal(layout.navigation, 0, width + 'px has no grouped navigation, group counts or section-selection attributes');
    assert.equal(layout.wrapper, false, width + 'px has no replacement navigation layout wrapper');
    assert.deepEqual(layout.headings, [], width + 'px has no former group headings');
    assert.equal(layout.inlineStyles, 0, width + 'px has no inline style attributes');
    assert.deepEqual(layout.columns.map(column=>column.className), ['col col-team','col col-work','col col-feed'], width + 'px retains the original sibling team, coordination and feed columns');
    for (const panel of layout.panels) assert.equal(panel.visible, true, width + 'px keeps ' + panel.id + ' accessible without selecting a group');
    if (width > 1280) {
      assert.equal(layout.tracks.length, 3, width + 'px uses the original three-column workspace');
      assert.equal(layout.tracks[0], 260, 'desktop team column stays 260px');
      assert.equal(layout.tracks[2], 300, 'desktop feed column stays 300px');
      assert.ok(layout.columns[0].left < layout.columns[1].left && layout.columns[1].left < layout.columns[2].left, 'desktop preserves left team / central work / right feed layout');
      assert.ok(layout.columns.every(column=>Math.abs(column.top-layout.columns[0].top)<1), 'desktop columns start together, rather than below the board');
    } else if (width <= 760) {
      assert.equal(layout.tracks.length, 1, width + 'px stacks the original panels');
      assert.ok(layout.columns.every(column=>column.display==='contents'), width + 'px preserves the non-group mobile layout');
    } else {
      assert.equal(layout.tracks.length, 2, width + 'px uses the original two-column responsive workspace');
      assert.equal(layout.tracks[0], 280, width + 'px responsive team column stays 280px');
      assert.ok(layout.columns[2].top > layout.columns[1].top, width + 'px activity moves below the main columns');
    }
    evidence.layouts.push({width,...layout});
    await noHorizontalScroll(width, 'ungrouped layout');
  }
  async function checkBoardLayout(width, expectedColumns) {
    await setViewport(width);
    await cdp.evaluate('window.scrollTo(0, 0)');
    await click('board-view');
    await waitFor(`!document.getElementById('board-panel').hidden && document.getElementById('timeline-panel').hidden`, width + 'px board view selection');
    await waitFor(`document.querySelectorAll('#tasks .board-column').length===4`, width + 'px board columns loaded');
    const layout = await cdp.evaluate(`(() => {
      const panel = document.getElementById('board-panel').getBoundingClientRect();
      const board = document.getElementById('tasks').getBoundingClientRect();
      const columns = Array.from(document.querySelectorAll('#tasks .board-column')).filter(column=>column.getClientRects().length).map((column, index) => {
        const rect = column.getBoundingClientRect();
        return { index, left: Math.round(rect.left), top: Math.round(rect.top), width: Math.round(rect.width), bottom: Math.round(rect.bottom) };
      });
      const uniqueLefts = [...new Set(columns.map(column => column.left))];
      const uniqueTops = [...new Set(columns.map(column => column.top))];
      return {
        panelLeft: Math.round(panel.left),
        panelWidth: Math.round(panel.width),
        boardLeft: Math.round(board.left),
        boardWidth: Math.round(board.width),
        columns,
        uniqueLefts,
        uniqueTops,
      };
    })()`);
    assert.equal(layout.columns.length, 4, width + 'px shows the four workflow columns');
    assert.equal(layout.uniqueLefts.length, expectedColumns, width + 'px board uses ' + expectedColumns + ' readable column tracks');
    assert.ok(layout.columns.every(column => column.width >= 200), width + 'px board columns remain readable inside the restored center column (' + JSON.stringify(layout.columns) + ')');
    assert.ok(layout.boardLeft >= layout.panelLeft && layout.boardLeft + layout.boardWidth <= layout.panelLeft + layout.panelWidth + 1, width + 'px board stays inside its panel');
    if (expectedColumns === 2) {
      assert.equal(layout.uniqueTops.length, 2, width + 'px board steps down to two rows of columns');
    } else if (expectedColumns === 1) {
      assert.equal(layout.uniqueTops.length, 4, width + 'px board stacks all four columns vertically');
    }
    await noHorizontalScroll(width, 'board layout');
  }

  try {
    await cdp.call('Page.enable'); await cdp.call('Runtime.enable'); await cdp.call('Network.enable');
    await cdp.call('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
    await cdp.call('Emulation.setTimezoneOverride', { timezoneId: 'UTC' });
    await cdp.call('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }] });
    await cdp.call('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: settings.downloads, eventsEnabled: true });
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', { source: `
      window.__cspViolations = [];
      document.addEventListener('securitypolicyviolation',event=>window.__cspViolations.push(event.violatedDirective));
      window.__notifications = []; window.__permissionRequests = 0;
      class SnapshotNotification {
        static permission = 'default';
        static requestPermission() { window.__permissionRequests++; this.permission='granted'; return Promise.resolve('granted'); }
        constructor(title, options) { window.__notifications.push({ title, options }); }
        close() {}
      }
      window.Notification = SnapshotNotification;
      window.__storageWrites = [];
      const originalSetItem = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        window.__storageWrites.push({ key: String(key), value: String(value) });
        return originalSetItem.call(this, key, value);
      };
    ` });
    await cdp.call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    await cdp.call('Page.navigate', { url: origin.href });
    await waitFor(`Boolean(document.getElementById('access-key')) && !document.getElementById('auth').hidden`, 'fresh browser profile shows the local login form');
    assert.equal(await cdp.evaluate(`document.getElementById('dashboard-tools').hidden`), true, 'tools stay hidden before authentication');
    assert.equal((await cdp.call('Network.getCookies', { urls: [origin.href] })).cookies.some(cookie => cookie.name === 'agentmesh_session'), false, 'fresh browser profile has no session cookie');
    await set('access-key', 'rejected-local-snapshot-' + 'z'.repeat(40));
    await cdp.evaluate(`document.getElementById('access-form').requestSubmit()`);
    await waitFor(`!!document.getElementById('auth-error').textContent && !document.getElementById('auth').hidden`, 'rejected login stays on the sign-in form');
    assert.equal((await cdp.call('Network.getCookies', { urls: [origin.href] })).cookies.some(cookie => cookie.name === 'agentmesh_session'), false, 'rejected login clears its session cookie');
    assert.equal(await cdp.evaluate('window.__permissionRequests'), 0, 'notifications never prompt on load');
    await set('access-key', settings.credential);
    await cdp.evaluate(`document.getElementById('access-form').requestSubmit()`);
    await waitFor(`document.getElementById('auth').hidden && document.querySelectorAll('#tasks .task-card').length===5 && document.getElementById('messages').textContent.includes('Cosmic transcript marker')`, 'snapshot dashboard');
    await waitFor(`Boolean(document.querySelector('.team-note') && document.querySelector('.task-handoff'))`, 'composed home notes and handoffs');
    assert.equal(await cdp.evaluate('window.__notifications.length'), 0, 'notifications off by default');
    assert.equal(await cdp.evaluate(`document.getElementById('dashboard-tools').hidden`), false);
    assert.ok(await cdp.evaluate(`document.getElementById('working-list').textContent.includes('T-102') && document.getElementById('working-list').textContent.includes('agent-a')`), 'working indicator identifies the real agent and task');
    assert.ok(await cdp.evaluate(`getComputedStyle(document.querySelector('.heartbeat[data-member-id="member-a"]')).boxShadow !== 'none'`), 'online working agent has a visible glow');
    assert.equal(await cdp.evaluate(`getComputedStyle(document.querySelector('.heartbeat[data-member-id="member-b"]')).boxShadow`), 'none', 'stale heartbeat never glows as active');
    assert.ok(await cdp.evaluate(`document.getElementById('messages').textContent.includes(${literal(settings.malicious)})`), 'malicious message is literal text');
    assert.equal(await cdp.evaluate(`Boolean(document.querySelector('#fixture-injection') || window.__injected)`), false, 'no executable malicious DOM');
    assert.equal(await cdp.evaluate(`document.documentElement.outerHTML.includes(${literal(settings.credential)})`), false, 'credential never appears in HTML');

    await search('Aurora snapshot task', 'task');
    await search('Cosmic transcript marker', 'message');
    await search('Nebula note marker', 'note');
    await search('Orbit handoff marker', 'handoff');
    await search('Nebula huddle marker', 'huddle');
    await set('global-search', '');
    await set('task-filter', 'mine', 'change');
    assert.deepEqual(await cdp.evaluate(cards), ['T-102'], 'my open tasks omits done and other agents');
    await set('task-filter', 'blocked', 'change');
    assert.deepEqual(await cdp.evaluate(cards), ['T-104'], 'blocked includes unfinished dependencies');
    await set('task-filter', 'done-today', 'change');
    assert.deepEqual(await cdp.evaluate(cards), ['T-103'], 'done today uses the recorded task_done event');
    assert.match(await cdp.evaluate(`document.getElementById('filter-status').textContent`), /unavailable|missing|not recorded/i, 'missing completion time is explained, never guessed from updated_at');
    await set('saved-view-name', 'Local completed view'); await click('save-view');
    assert.ok(await cdp.evaluate(`Array.from(document.getElementById('saved-views').options).some(option=>option.textContent.includes('Local completed view'))`), 'saved view appears');
    const stored = await cdp.evaluate(`Object.fromEntries(Object.entries(localStorage))`);
    assert.ok(JSON.stringify(stored).includes('Local completed view'), 'only non-secret saved view preferences are persisted');
    assert.ok(!JSON.stringify(stored).includes(settings.credential), 'no credential in localStorage');
    await checkCookiePrivacy('typed sign-in');
    await set('saved-view-name', settings.credential); await click('save-view');
    assert.ok(!JSON.stringify(await cdp.evaluate(`Object.fromEntries(Object.entries(localStorage))`)).includes(settings.credential), 'pasting the active credential cannot persist it as a view name');
    await set('task-filter', 'all', 'change');
    const savedValue = await cdp.evaluate(`Array.from(document.getElementById('saved-views').options).find(option=>option.textContent.includes('Local completed view')).value`);
    await set('saved-views', savedValue, 'change');
    assert.equal(await cdp.evaluate(`document.getElementById('task-filter').value`), 'done-today');
    await click('delete-view');
    assert.equal(await cdp.evaluate(`Array.from(document.getElementById('saved-views').options).some(option=>option.textContent.includes('Local completed view'))`), false);
    await set('task-filter', 'all', 'change');

    await cdp.evaluate(`document.querySelector('#tasks .task-card').focus()`);
    const first = await cdp.evaluate('document.activeElement.id');
    await key('j');
    const second = await cdp.evaluate('document.activeElement.id');
    assert.ok(second && second !== first, 'j moves focus to next card');
    await key('k');
    assert.equal(await cdp.evaluate('document.activeElement.id'), first, 'k returns to previous card, including nested handoffs');
    await key('/', 'Slash');
    assert.equal(await cdp.evaluate('document.activeElement.id'), 'global-search', '/ focuses search');
    await key('j');
    assert.equal(await cdp.evaluate('document.activeElement.id'), 'global-search', 'card shortcuts do not interrupt typing');
    await cdp.evaluate('document.activeElement.blur()');
    await key('?', 'Slash');
    assert.equal(await cdp.evaluate(`document.getElementById('shortcuts').open`), true, '? opens shortcut help');
    const shortcuts = await cdp.evaluate(`Array.from(document.querySelectorAll('#shortcuts kbd'),node=>node.textContent)`);
    for (const shortcut of ['/', 'j', 'k', '?', 'Esc']) assert.ok(shortcuts.includes(shortcut), 'shortcut help retains ' + shortcut);
    assert.ok(!shortcuts.includes('[') && !shortcuts.includes(']'), 'shortcut help removes the group-only navigation keys');
    evidence.checks.push('j/k focus navigation, / search, typing guard, ? help and Escape');
    await click('close-shortcuts');
    await cdp.evaluate(`document.querySelector('#tasks .task-card').focus()`);
    const ungroupedFocus = await cdp.evaluate('document.activeElement.id');
    for (const [pressed, code] of [['[', 'BracketLeft'], [']', 'BracketRight']]) {
      await key(pressed, code);
      assert.equal(await cdp.evaluate('document.activeElement.id'), ungroupedFocus, pressed + ' no longer changes dashboard navigation or focus');
      assert.equal(await cdp.evaluate(`localStorage.getItem('agentmesh:current-section')`), null, pressed + ' does not persist removed group selection');
    }

    for (const [width, expectedColumns] of [[1440, 2], [1000, 2], [760, 2], [640, 2], [390, 1]]) {
      await checkBoardLayout(width, expectedColumns);
      await checkUngroupedLayout(width);
    }
    for (const width of [1440, 390]) {
      await setViewport(width);
      await checkUngroupedLayout(width);
      await click('board-view'); await delay(150);
      assert.equal(await cdp.evaluate(`document.getElementById('board-view').getAttribute('aria-pressed')`), 'true', 'Board toolbar retains its view selection');
      await screenshot('dashboard-' + width + '-board');
      await noHorizontalScroll(width, 'board');
      await click('timeline-view');
      assert.equal(await cdp.evaluate(`document.getElementById('timeline-view').getAttribute('aria-pressed')`), 'true', 'Timeline toolbar retains its view selection');
      await waitFor(`!document.getElementById('timeline-panel').hidden && document.querySelectorAll('#timeline-track .timeline-entry').length>=5`, width + 'px timeline');
      const timeline = await cdp.evaluate(`document.getElementById('timeline-track').textContent`);
      assert.ok(timeline.includes('T-101') && timeline.includes('Cosmic transcript marker'), 'timeline includes loaded tasks and messages');
      const times = await cdp.evaluate(`Array.from(document.querySelectorAll('#timeline-track .timeline-entry time'),time=>Date.parse(time.dateTime))`);
      assert.ok(times.every((time, index) => Number.isFinite(time) && (!index || time >= times[index - 1])), 'timeline entries use real chronological timestamps');
      await noHorizontalScroll(width, 'timeline');
      await cdp.evaluate('window.scrollTo(0,0)');
      await screenshot('dashboard-' + width + '-timeline');
      evidence.checks.push(width + 'px board/timeline and no horizontal page scroll');
      await set('global-search', 'Aurora');
      assert.ok(await cdp.evaluate(`document.querySelector('#search-results a[data-kind="task"]') !== null`), 'mobile search remains usable');
      await noHorizontalScroll(width, 'search');
      await search('Aurora snapshot task', 'task');
      await search('Cosmic transcript marker', 'message');
      await search('Orbit handoff marker', 'handoff');
      await set('global-search', '');
    }
    assert.equal(await cdp.evaluate(`document.getElementById('handoff-count').textContent.trim()`), '1', 'Handoffs panel count matches its one loaded handoff');
    assert.ok(await cdp.evaluate(`document.getElementById('handoffs-panel').textContent.includes('Orbit handoff marker')`), 'standalone Handoffs section retains loaded handoff data');
    await cdp.call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    await click('board-view');
    const beforeTheme = await cdp.evaluate(`document.documentElement.getAttribute('data-theme')`);
    await click('theme-toggle');
    assert.notEqual(await cdp.evaluate(`document.documentElement.getAttribute('data-theme')`), beforeTheme, 'existing theme toggle works');

    for (const button of ['export-board', 'export-transcript']) {
      const before = new Set(fs.readdirSync(settings.downloads));
      await click(button);
      let downloaded;
      const deadline = Date.now() + 10000;
      while (Date.now() < deadline) {
        downloaded = fs.readdirSync(settings.downloads).find(name => !before.has(name) && name.endsWith('.md'));
        if (downloaded) break;
        await delay(50);
      }
      assert.ok(downloaded, button + ' downloads a Markdown file');
      const markdown = fs.readFileSync(path.join(settings.downloads, downloaded), 'utf8');
      assert.ok(markdown.includes('T-101'), button + ' preserves T-keys');
      assert.ok(markdown.includes(button === 'export-board' ? 'Aurora snapshot task' : 'Cosmic transcript marker'), button + ' contains loaded data');
      assert.ok(!markdown.includes(settings.credential), 'export never contains credentials');
      fs.copyFileSync(path.join(settings.downloads, downloaded), path.join(settings.evidence, button + '.md'));
      evidence.exports.push(button + '.md');
    }

    await click('notify-toggle');
    await waitFor('window.__permissionRequests===1', 'explicit notification permission button');
    await cdp.evaluate(`(() => {
      window.__motion = { animations: [], slides: [] };
      document.addEventListener('animationstart', event => window.__motion.animations.push({ id:event.target.id, name:event.animationName }), true);
      const animate = Element.prototype.animate;
      Element.prototype.animate = function(...args) { window.__motion.slides.push(this.dataset.taskKey); return animate.apply(this,args); };
    })()`);
    await cdp.evaluate(`fetch('/__fixture/advance').then(response=>response.json())`);
    await waitFor(`document.getElementById('questions').textContent.includes('Photon question marker')`, 'incoming local question');
    await waitFor('window.__notifications.length>=2', 'targeted incoming question and task notifications');
    assert.ok((await cdp.evaluate('JSON.stringify(window.__notifications)')).includes('T-101'), 'assignment notification identifies its task');
    await waitFor(`window.__motion.animations.some(event=>event.id==='item-task-T-101' && event.name==='task-arrival')`, 'status-change task flash');
    assert.ok(await cdp.evaluate(`window.__motion.slides.includes('T-101')`), 'status-change task animates from its former board position');
    await waitFor(`window.__motion.animations.some(event=>event.id==='item-message-message-incoming' && event.name==='activity-ripple')`, 'incoming message ripple');
    assert.ok(await cdp.evaluate(`window.__motion.animations.some(event=>event.id==='member-heartbeats' && event.name==='activity-ripple')`), 'incoming task/message event ripples the live agents');
    await search('Photon question marker', 'question');
    await set('global-search', '');
    const notificationCount = await cdp.evaluate('window.__notifications.length');
    await delay(4300);
    assert.equal(await cdp.evaluate('window.__notifications.length'), notificationCount, 'unchanged refresh never duplicates notifications');
    await click('notify-toggle');

    await cdp.call('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
    await cdp.evaluate(`window.__motion.animations=[]; window.__motion.slides=[]`);
    await cdp.evaluate(`fetch('/__fixture/advance').then(response=>response.json())`);
    await waitFor(`Boolean(document.getElementById('item-message-message-reduced'))`, 'reduced-motion incoming snapshot');
    await delay(200);
    assert.deepEqual(await cdp.evaluate('window.__motion.animations'), [], 'reduced-motion preference suppresses CSS motion');
    assert.deepEqual(await cdp.evaluate('window.__motion.slides'), [], 'reduced-motion preference suppresses task slides');
    assert.equal(await cdp.evaluate('window.__notifications.length'), notificationCount, 'disabled notifications stay off on later events');

    assert.deepEqual(await cdp.evaluate('window.__cspViolations'), [], 'all feature interactions have no CSP violations');
    await click('board-view');
    await cdp.call('Emulation.setDeviceMetricsOverride', { width: 390, height: 1000, deviceScaleFactor: 1, mobile: true });
    await cdp.evaluate('window.__beforeReload=true');
    await cdp.call('Page.reload');
    await waitFor(`window.__beforeReload!==true && document.getElementById('auth')?.hidden && document.querySelectorAll('#tasks .task-card').length===5 && document.getElementById('messages').textContent.includes('Cosmic transcript marker')`, 'reload restores the HttpOnly cookie session without prompting');
    await waitFor(`!!document.querySelector('.team-note') && document.getElementById('handoffs-panel').textContent.includes('Orbit handoff marker')`, 'reload restores retained notes and handoffs');
    await checkUngroupedLayout(390);
    await noHorizontalScroll(390, 'restored mobile dashboard');
    evidence.checks.push('cookie sign-in survives reload with notes and handoffs accessible');
    assert.equal(await cdp.evaluate('window.__permissionRequests'), 0, 'reload never requests permission');
    assert.equal(await cdp.evaluate('window.__notifications.length'), 0, 'restoring the page does not notify for its existing records');
    await checkCookiePrivacy('reload');
    assert.ok(!JSON.stringify(await cdp.evaluate(`Object.fromEntries(Object.entries(localStorage))`)).includes(settings.credential), 'cookie persistence never places the credential in localStorage');
    assert.equal(await cdp.evaluate(`document.documentElement.outerHTML.includes(${literal(settings.credential)}) || location.href.includes(${literal(settings.credential)})`), false, 'restored credential stays outside HTML and URL');
    // Closing the authenticated tab must not end a persistent browser session.
    assert.deepEqual(cdp.remoteRequests, [], 'initial tab opens no new network hosts');
    assert.deepEqual(cdp.exceptions, [], 'initial tab has no uncaught browser exceptions');
    assert.deepEqual(await cdp.evaluate('window.__cspViolations'), [], 'initial tab has no CSP violations');
    await browser.call('Target.closeTarget', { targetId: currentTarget });
    cdp.close();
    const replacement = await openPage();
    cdp = replacement.cdp; currentTarget = replacement.targetId;
    await waitFor(`document.getElementById('auth')?.hidden && document.querySelectorAll('#tasks .task-card').length===5`, 'new tab uses the cookie after the old tab was closed');
    await checkCookiePrivacy('new tab after tab closure');

    // An isolated, fresh browser profile must not inherit the logged-in profile.
    const { browserContextId } = await browser.call('Target.createBrowserContext');
    const fresh = await openPage(browserContextId);
    try {
      const deadline = Date.now() + 15000;
      while (!(await fresh.cdp.evaluate(`document.getElementById('auth')?.hidden===false && document.getElementById('workspace')?.hidden===true`))) {
        assert.ok(Date.now() < deadline, 'fresh browser profile shows sign-in');
        await delay(60);
      }
      assert.equal((await fresh.cdp.call('Network.getCookies', { urls: [origin.href] })).cookies.some(cookie => cookie.name === 'agentmesh_session'), false, 'fresh profile has no session cookie');
      assert.deepEqual(fresh.cdp.remoteRequests, [], 'fresh profile opens no new network hosts');
      assert.deepEqual(fresh.cdp.exceptions, [], 'fresh profile has no uncaught browser exceptions');
      assert.deepEqual(await fresh.cdp.evaluate('window.__cspViolations'), [], 'fresh profile has no CSP violations');
    } finally {
      fresh.cdp.close();
      await browser.call('Target.disposeBrowserContext', { browserContextId });
    }
    await click('sign-out');
    await waitFor(`!document.getElementById('auth').hidden`, 'explicit sign out returns to authentication');
    assert.equal((await cdp.call('Network.getCookies', { urls: [origin.href] })).cookies.some(cookie => cookie.name === 'agentmesh_session'), false, 'sign out clears the persistent cookie');
    assert.equal(await cdp.evaluate(`sessionStorage.getItem('agentmesh:session')`), null);
    assert.ok(!JSON.stringify(await cdp.evaluate(`({local:Object.fromEntries(Object.entries(localStorage)),session:Object.fromEntries(Object.entries(sessionStorage))})`)).includes(settings.credential));
    await cdp.evaluate('window.__beforeReload=true');
    await cdp.call('Page.reload');
    await waitFor(`window.__beforeReload!==true && document.getElementById('auth') && !document.getElementById('auth').hidden`, 'signed-out reload still requires authentication');
    assert.equal(await cdp.evaluate(`sessionStorage.getItem('agentmesh:session')`), null, 'sign out remains effective after reload');
    assert.deepEqual(cdp.remoteRequests, [], 'dashboard opens no new network hosts');
    assert.deepEqual(cdp.exceptions, [], 'dashboard has no uncaught browser exceptions');
    for (const session of sessions) assert.deepEqual(session.consoleErrors, [], 'all tabs have no console errors');
    assert.deepEqual(await cdp.evaluate('window.__cspViolations || []'), [], 'no CSP violations');
    evidence.checks.push('saved filters/views, notifications off by default and explicit toggle, reduced motion, theme, handoffs/notes, persistent HttpOnly cookie sessions, fresh profile and logout');
    evidence.result = 'passed';
    console.log('dashboard-polish-chrome-ok: local snapshot, search, exports, shortcuts, timeline, filters, notifications, mobile, restored ungrouped columns, persistent HttpOnly cookie sessions and fresh profiles');
  } catch (error) {
    evidence.error = String(error.stack || error).replaceAll(settings.credential, '[redacted]');
    await screenshot('polish-failure').catch(() => {});
    throw error;
  } finally {
    evidence.requests = sessions.flatMap(session=>session.requests);
    evidence.exceptions = sessions.flatMap(session=>session.exceptions);
    evidence.consoleErrors = sessions.flatMap(session=>session.consoleErrors);
    evidence.remoteRequests = sessions.flatMap(session=>session.remoteRequests);
    fs.writeFileSync(path.join(settings.evidence, 'polish-evidence.json'), JSON.stringify(evidence, null, 2) + '\n');
    for (const session of sessions) session.close();
    browser.close();
  }
}

module.exports = { connect };
if (require.main === module) {
  (async () => {
    let input = '';
    for await (const chunk of process.stdin) input += chunk;
    await main(JSON.parse(input));
  })().catch(error => { console.error(error); process.exitCode = 1; });
}
