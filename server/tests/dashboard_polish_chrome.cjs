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
  const pending = new Map(), remoteRequests = [], exceptions = [], requests = [];
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
    call, remoteRequests, exceptions, requests,
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
  async function mouseClick(x, y) {
    await cdp.call('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none' });
    await cdp.call('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await cdp.call('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
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
    if (await cdp.evaluate('document.documentElement.clientWidth <= 600')) {
      assert.ok(await cdp.evaluate(`(() => { const section = document.querySelector('#section-nav [aria-current="page"]')?.dataset.section; const visible=Array.from(document.querySelectorAll('.panel[data-section]')).filter(panel=>panel.getClientRects().length); return !!section && visible.length>0 && visible.every(panel=>panel.dataset.section===section); })()`), kind + ' search selects its containing phone section');
    }
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
  const navigationGroups = [
    { title: 'Work', sections: ['board', 'timeline', 'plans', 'claims'] },
    { title: 'Conversation', sections: ['transcript', 'questions', 'huddles'] },
    { title: 'Knowledge', sections: ['notes', 'handoffs', 'patches'] },
    { title: 'Team and activity', sections: ['members', 'activity', 'rules'] },
  ];
  const sectionCounts = {
    board: 'task-count', timeline: 'timeline-count', plans: 'master-count', claims: 'claim-count',
    transcript: 'message-count', questions: 'question-count', huddles: 'huddle-count',
    notes: 'team-notes-count', handoffs: 'handoff-count', patches: 'patch-count',
    members: 'team-count', activity: 'activity-count', rules: 'rules-count',
  };
  async function selectSection(section) {
    const interaction = await cdp.evaluate(`(() => {
      window.scrollTo(0, 0);
      const nav = document.getElementById('section-nav');
      const link = nav?.querySelector('[data-section-link][data-section="${section}"]');
      if (!nav || !link) return null;
      const before = nav.scrollLeft;
      link.scrollIntoView({ block: 'nearest', inline: 'center' });
      const navRect = nav.getBoundingClientRect();
      const rect = link.getBoundingClientRect();
      const x = Math.max(navRect.left + 8, Math.min(rect.left + rect.width / 2, navRect.right - 8));
      const y = Math.max(navRect.top + 8, Math.min(rect.top + rect.height / 2, navRect.bottom - 8));
      const hit = document.elementFromPoint(x, y);
      return {
        x,
        y,
        before,
        after: nav.scrollLeft,
        navClientWidth: nav.clientWidth,
        navScrollWidth: nav.scrollWidth,
        hitSection: hit?.closest('[data-section-link]')?.dataset.section || null,
        hitInsideNav: !!hit?.closest('#section-nav'),
      };
    })()`);
    assert.ok(interaction, section + ' navigation link exists');
    assert.equal(interaction.hitInsideNav, true, section + ' navigation hit target stays inside the sticky bar');
    assert.equal(interaction.hitSection, section, section + ' navigation hit test reaches the intended tab');
    await mouseClick(interaction.x, interaction.y);
    await waitFor(`document.querySelector('#section-nav [data-section-link][aria-current="page"]')?.dataset.section===${literal(section)}`, section + ' navigation selection');
    return interaction;
  }
  async function checkSectionNavigation(width) {
    await cdp.evaluate('window.scrollTo(0, 0)');
    await click('board-view');
    await waitFor(`document.querySelector('#section-nav [aria-current="page"]')?.dataset.section==='board'`, width + 'px board section before sticky navigation checks');
    const nav = await cdp.evaluate(`(() => {
      const nav=document.getElementById('section-nav');
      const content=document.querySelector('.dashboard-content');
      const links=Array.from(nav?.querySelectorAll('[data-section-link]')||[]);
      const linkTops=[...new Set(links.map(link=>Math.round(link.getBoundingClientRect().top)))];
      return {
        visible:!!nav?.getClientRects().length,
        groups:Array.from(nav?.querySelectorAll('.nav-group')||[],group=>{const heading=group.querySelector('h2,h3,h4')?.cloneNode(true);heading?.querySelectorAll('.group-count').forEach(count=>count.remove());return {heading:heading?.textContent.trim(),count:group.querySelector('.group-count')?.textContent.trim(),sections:Array.from(group.querySelectorAll('[data-section-link]'),link=>link.dataset.section)};}),
        sections:links.map(link=>link.dataset.section),
        scrollable:nav ? nav.scrollWidth > nav.clientWidth + 1 : false,
        wraps:linkTops.length > 1,
        navLeft:nav?.getBoundingClientRect().left,
        navWidth:nav?.getBoundingClientRect().width,
        contentLeft:content?.getBoundingClientRect().left,
        contentWidth:content?.getBoundingClientRect().width,
      };
    })()`);
    assert.equal(nav.visible, true, width + 'px section navigation is visible');
    assert.deepEqual(nav.groups, navigationGroups.map(group=>({heading:group.title,count:String(group.sections.length),sections:group.sections})), width + 'px navigation has four named groups and exact section counts');
    assert.deepEqual(nav.sections, Object.keys(sectionCounts), width + 'px every section has exactly one navigation entry');
    assert.ok(Math.abs(nav.navLeft - nav.contentLeft) <= 1, width + 'px sticky navigation aligns with the dashboard content left edge');
    assert.ok(Math.abs(nav.navWidth - nav.contentWidth) <= 2, width + 'px sticky navigation spans the dashboard content width');
    const position = await cdp.evaluate(`(() => {
      const nav=document.getElementById('section-nav').getBoundingClientRect();
      const panels=Array.from(document.querySelectorAll('.panel[data-section]')).filter(panel=>panel.getClientRects().length).map(panel=>panel.getBoundingClientRect());
      return {navBottom:nav.bottom,panelsTop:Math.min(...panels.map(panel=>panel.top)),sticky:getComputedStyle(document.getElementById('section-nav')).position};
    })()`);
    assert.equal(position.sticky, 'sticky', width + 'px section navigation stays sticky');
    assert.ok(position.navBottom <= position.panelsTop + 1, width + 'px section navigation sits above the dashboard panels');
    const sticky = await cdp.evaluate(`(() => {
      const nav=document.getElementById('section-nav');
      const naturalTop=Math.round(nav.getBoundingClientRect().top + window.scrollY);
      const maxScroll=document.documentElement.scrollHeight - window.innerHeight;
      const firstTarget=Math.min(maxScroll, naturalTop + 120);
      window.scrollTo(0, firstTarget);
      const stuck=nav.getBoundingClientRect().top;
      const secondTarget=Math.min(maxScroll, naturalTop + 240);
      window.scrollTo(0, secondTarget);
      const stillStuck=nav.getBoundingClientRect().top;
      window.scrollTo(0, 0);
      return {naturalTop, firstTarget, secondTarget, stuck, stillStuck, maxScroll};
    })()`);
    assert.ok(sticky.naturalTop > 12, width + 'px navigation begins below the top of the page before sticking');
    assert.ok(sticky.firstTarget > sticky.naturalTop, width + 'px page can scroll past the natural navigation position');
    assert.ok(Math.abs(sticky.stuck - 12) <= 4, width + 'px navigation stays sticky after page scroll');
    if (sticky.secondTarget > sticky.naturalTop) assert.ok(Math.abs(sticky.stillStuck - 12) <= 4, width + 'px navigation remains sticky deeper into the page');
    if (width === 390) assert.ok(nav.scrollable || nav.wraps, '390px navigation stays usable by wrapping or scrolling inside the sticky bar');
    for (const section of Object.keys(sectionCounts)) {
      if (width === 390 && section === 'rules') await cdp.evaluate(`document.getElementById('section-nav').scrollLeft = 0`);
      const interaction = await selectSection(section);
      const state = await cdp.evaluate(`(() => {
        const nav=document.getElementById('section-nav'),links=Array.from(nav.querySelectorAll('[data-section-link]'));
        const link=links.find(link=>link.dataset.section===${literal(section)}),counter=document.getElementById(${literal(sectionCounts[section])});
        const visible=Array.from(document.querySelectorAll('.panel[data-section]')).filter(panel=>panel.getClientRects().length);
        return {current:links.filter(link=>link.getAttribute('aria-current')==='page').map(link=>link.dataset.section),badge:link.querySelector('.nav-count')?.textContent.trim(),count:counter?.textContent.trim(),stored:localStorage.getItem('agentmesh:current-section'),visible:visible.map(panel=>panel.dataset.section),linkVisible:!!link.getClientRects().length,targetExists:!!document.getElementById(decodeURIComponent(link.hash.slice(1))),workingListVisible:!!document.getElementById('working-list')?.getClientRects().length,workingText:document.getElementById('working-list')?.textContent || ''};
      })()`);
      assert.deepEqual(state.current, [section], width + 'px ' + section + ' is the only aria-current entry');
      assert.equal(state.linkVisible, true, width + 'px ' + section + ' link remains usable');
      assert.equal(state.targetExists, true, width + 'px ' + section + ' link resolves to a real section');
      assert.notEqual(state.count, undefined, section + ' has a real panel count');
      assert.equal(state.badge, state.count, width + 'px ' + section + ' badge matches its panel');
      assert.equal(state.stored, section, width + 'px current section is remembered as a non-secret identifier');
      assert.ok(state.visible.includes(section), width + 'px ' + section + ' is reachable');
      if (width === 390) {
        assert.ok(state.visible.every(value=>value===section), 'phone shows only the selected ' + section + ' section');
        if (section === 'members') {
          assert.equal(state.workingListVisible, true, 'phone members section exposes the working-now list');
          assert.ok(state.workingText.includes('T-102') && state.workingText.includes('agent-a'), 'phone members section keeps the working-now content');
        }
        if (section === 'rules' && interaction.navScrollWidth > interaction.navClientWidth + 1) {
          assert.ok(interaction.after > interaction.before && interaction.after > 0, '390px navigation scrolls inside the sticky bar to reach later tabs');
        }
      } else {
        for (const visibleSection of Object.keys(sectionCounts).filter(value=>value!=='timeline')) assert.ok(state.visible.includes(visibleSection), 'desktop keeps ' + visibleSection + ' in its compact overview');
      }
      await noHorizontalScroll(width, section + ' navigation');
    }
    await selectSection('board');
    await cdp.evaluate('document.activeElement.blur()');
    for (const [pressed, code, sections] of [
      [']', 'BracketRight', ['transcript', 'notes', 'members', 'board']],
      ['[', 'BracketLeft', ['members', 'notes', 'transcript', 'board']],
    ]) {
      for (const section of sections) {
        await key(pressed, code);
        assert.equal(await cdp.evaluate(`document.querySelector('#section-nav [aria-current="page"]')?.dataset.section`), section, width + 'px ' + pressed + ' moves between groups and wraps');
      }
    }
  }
  async function checkBoardLayout(width, expectedColumns) {
    await setViewport(width);
    await cdp.evaluate('window.scrollTo(0, 0)');
    await click('board-view');
    await waitFor(`document.querySelector('#section-nav [aria-current="page"]')?.dataset.section==='board'`, width + 'px board view selection');
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
      const docTop = rect => Math.round(rect.top + window.scrollY);
      return {
        panelLeft: Math.round(panel.left),
        panelWidth: Math.round(panel.width),
        boardLeft: Math.round(board.left),
        boardWidth: Math.round(board.width),
        boardBottom: Math.round(board.bottom + window.scrollY),
        columns,
        uniqueLefts,
        uniqueTops,
        membersTop: docTop(document.getElementById('members-panel').getBoundingClientRect()),
        workingTop: docTop(document.getElementById('working-list').getBoundingClientRect()),
        activityTop: docTop(document.getElementById('activity-panel').getBoundingClientRect()),
      };
    })()`);
    assert.equal(layout.columns.length, 4, width + 'px shows the four workflow columns');
    assert.equal(layout.uniqueLefts.length, expectedColumns, width + 'px board uses ' + expectedColumns + ' readable column tracks');
    assert.ok(layout.columns.every(column => column.width >= 280), width + 'px board columns stay at least about 280px wide (' + JSON.stringify(layout.columns) + ')');
    assert.ok(layout.boardLeft >= layout.panelLeft && layout.boardLeft + layout.boardWidth <= layout.panelLeft + layout.panelWidth + 1, width + 'px board stays inside its panel');
    if (expectedColumns === 4) {
      assert.equal(layout.uniqueTops.length, 1, '1440px board keeps all four workflow columns on the same row');
      assert.ok(layout.membersTop >= layout.boardBottom, '1440px members panel sits below the board');
      assert.ok(layout.workingTop >= layout.boardBottom, '1440px working-now content sits below the board');
      assert.ok(layout.activityTop >= layout.boardBottom, '1440px activity panel sits below the board');
    } else if (expectedColumns === 2) {
      assert.equal(layout.uniqueTops.length, 2, width + 'px board steps down to two rows of columns');
    } else if (expectedColumns === 1) {
      assert.ok(layout.uniqueTops.length >= 2, width + 'px board stacks its columns vertically');
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
    assert.ok(await cdp.evaluate(`document.getElementById('shortcuts').textContent.includes('[') && document.getElementById('shortcuts').textContent.includes(']')`), 'shortcut help documents both group navigation keys');
    await click('close-shortcuts');

    for (const [width, expectedColumns] of [[1440, 4], [1000, 2], [760, 2], [640, 1], [390, 1]]) {
      await checkBoardLayout(width, expectedColumns);
    }
    for (const width of [1440, 390]) {
      await setViewport(width);
      await checkSectionNavigation(width);
      await click('board-view'); await delay(150);
      assert.equal(await cdp.evaluate(`document.querySelector('#section-nav [aria-current="page"]')?.dataset.section`), 'board', 'Board toolbar and navigation selection stay synchronized');
      await noHorizontalScroll(width, 'board');
      await click('timeline-view');
      assert.equal(await cdp.evaluate(`document.querySelector('#section-nav [aria-current="page"]')?.dataset.section`), 'timeline', 'Timeline toolbar and navigation selection stay synchronized');
      await waitFor(`!document.getElementById('timeline-panel').hidden && document.querySelectorAll('#timeline-track .timeline-entry').length>=5`, width + 'px timeline');
      const timeline = await cdp.evaluate(`document.getElementById('timeline-track').textContent`);
      assert.ok(timeline.includes('T-101') && timeline.includes('Cosmic transcript marker'), 'timeline includes loaded tasks and messages');
      const times = await cdp.evaluate(`Array.from(document.querySelectorAll('#timeline-track .timeline-entry time'),time=>Date.parse(time.dateTime))`);
      assert.ok(times.every((time, index) => Number.isFinite(time) && (!index || time >= times[index - 1])), 'timeline entries use real chronological timestamps');
      await noHorizontalScroll(width, 'timeline');
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

    await selectSection('handoffs');
    await cdp.call('Emulation.setDeviceMetricsOverride', { width: 390, height: 1000, deviceScaleFactor: 1, mobile: true });
    await cdp.evaluate('window.__beforeReload=true');
    await cdp.call('Page.reload');
    await waitFor(`window.__beforeReload!==true && document.getElementById('auth')?.hidden && document.querySelectorAll('#tasks .task-card').length===5 && document.getElementById('messages').textContent.includes('Cosmic transcript marker')`, 'reload restores the HttpOnly cookie session without prompting');
    await waitFor(`document.querySelector('#section-nav [aria-current="page"]')?.dataset.section==='handoffs'`, 'reload restores the current section');
    assert.ok(await cdp.evaluate(`(() => { const visible=Array.from(document.querySelectorAll('.panel[data-section]')).filter(panel=>panel.getClientRects().length); return visible.length>0 && visible.every(panel=>panel.dataset.section==='handoffs'); })()`), 'restored phone section remains isolated');
    await noHorizontalScroll(390, 'restored section');
    assert.equal(await cdp.evaluate('window.__permissionRequests'), 0, 'reload never requests permission');
    assert.equal(await cdp.evaluate('window.__notifications.length'), 0, 'restoring the page does not notify for its existing records');
    await checkCookiePrivacy('reload');
    assert.ok(!JSON.stringify(await cdp.evaluate(`Object.fromEntries(Object.entries(localStorage))`)).includes(settings.credential), 'cookie persistence never places the credential in localStorage');
    assert.equal(await cdp.evaluate(`document.documentElement.outerHTML.includes(${literal(settings.credential)}) || location.href.includes(${literal(settings.credential)})`), false, 'restored credential stays outside HTML and URL');
    // Closing the authenticated tab must not end a persistent browser session.
    assert.deepEqual(cdp.remoteRequests, [], 'initial tab opens no new network hosts');
    assert.deepEqual(cdp.exceptions, [], 'initial tab has no uncaught browser exceptions');
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
    console.log('dashboard-polish-chrome-ok: local snapshot, search, exports, shortcuts, timeline, filters, notifications, mobile, section navigation, persistent HttpOnly cookie sessions and fresh profiles');
  } finally { for (const session of sessions) session.close(); browser.close(); }
}

module.exports = { connect };
if (require.main === module) {
  (async () => {
    let input = '';
    for await (const chunk of process.stdin) input += chunk;
    await main(JSON.parse(input));
  })().catch(error => { console.error(error); process.exitCode = 1; });
}
