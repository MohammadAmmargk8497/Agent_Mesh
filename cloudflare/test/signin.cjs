const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const signin = fs.readFileSync(path.join(__dirname, '../src/signin.html'), 'utf8');
const dashboard = fs.readFileSync(path.join(__dirname, '../src/dashboard.html'), 'utf8');

function browser(fetchResponse, storageUnavailable = false) {
  const elements = new Map([...signin.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {
    value: '', textContent: '', disabled: false, listeners: {}, attributes: {},
    addEventListener(type, callback) { this.listeners[type] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; },
  }]));
  const attributes = { 'data-theme': 'light' };
  const document = {
    getElementById: id => { assert.ok(elements.has(id), 'missing element: ' + id); return elements.get(id); },
    documentElement: { getAttribute: name => attributes[name], setAttribute: (name, value) => { attributes[name] = value; } },
  };
  const storage = new Map([['papers-theme', 'dark']]), requests = [], redirects = [];
  const context = vm.createContext({
    document,
    localStorage: {
      getItem(key) { if (storageUnavailable) throw new Error('storage unavailable'); return storage.get(key); },
      setItem(key, value) { if (storageUnavailable) throw new Error('storage unavailable'); storage.set(key, value); },
    },
    location: { replace: url => redirects.push(url) },
    fetch: async (url, options) => { requests.push({ url, options }); return fetchResponse(url, options); },
  });
  for (const [, script] of signin.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/g)) vm.runInContext(script, context);
  return { elements, document, storage, requests, redirects, async submit(key, teamId = '') {
    elements.get('access-key').value = key;
    elements.get('team-id').value = teamId;
    let prevented = false;
    await elements.get('signin-form').listeners.submit({ preventDefault() { prevented = true; } });
    assert.equal(prevented, true, 'credentials must never use native form submission');
  } };
}

const stateResponse = (status, state) => ({ status, ok: status >= 200 && status < 300, json: async () => state });

module.exports = async function testSignin(worker, env) {
  // Public front-door pages keep their existing headers and inject only a fresh nonce.
  const nonces = [];
  for (const route of ['/', '/index.html', '/viewer', '/']) {
    const page = await worker.default.fetch(new Request('https://agentmesh.zahid.win' + route), env);
    assert.equal(page.status, 200, route);
    assert.equal(page.headers.get('content-type'), 'text/html; charset=utf-8');
    assert.equal(page.headers.get('cache-control'), 'no-store');
    assert.equal(page.headers.get('x-content-type-options'), 'nosniff');
    const html = await page.text();
    const csp = page.headers.get('content-security-policy');
    const nonce = /script-src 'nonce-([^']+)'/.exec(csp)[1];
    nonces.push(nonce);
    assert.equal(csp, `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'`);
    assert.equal(html, (route === '/viewer' ? dashboard : signin).replaceAll('__AGENTMESH_NONCE__', nonce));
    assert.ok([...html.matchAll(/<(?:script|style)\b[^>]*>/g)].every(([tag]) => tag.includes('nonce="' + nonce + '"')));
    for (const token of [env.AGENT_A_TOKEN, env.AGENT_B_TOKEN, env.VIEWER_TOKEN]) assert.ok(!html.includes(token));
    assert.ok(html.includes(route === '/viewer' ? 'id="viewer-form"' : 'id="signin-form"'));
  }
  assert.equal(new Set(nonces).size, nonces.length);
  for (const host of ['agents.zahid.win', 'mesh.example']) {
    for (const route of ['/', '/index.html', '/viewer']) {
      assert.equal((await worker.default.fetch(new Request('https://' + host + route), env)).status, 401);
    }
  }
  assert.match(signin, /<input\b[^>]*id="access-key"[^>]*type="password"[^>]*required/);
  assert.ok(!/\b(?:innerHTML|outerHTML|insertAdjacentHTML|createContextualFragment)\b/.test(signin));
  assert.ok(!/\bon\w+=/i.test(signin));
  assert.ok(!/<input\b[^>]*\bname=/i.test(signin), 'a failed script must not leak the key through a native GET');

  const key = 'key +/#?&=%';
  const app = browser(async () => stateResponse(200, { team: { id: 'tabc123' } }));
  assert.equal(app.document.documentElement.getAttribute('data-theme'), 'dark');
  app.elements.get('theme-toggle').listeners.click();
  assert.equal(app.document.documentElement.getAttribute('data-theme'), 'light');
  assert.equal(app.storage.get('papers-theme'), 'light');
  await app.submit(key);
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].url, '/api/state');
  assert.equal(app.requests[0].options.headers.Authorization, 'Bearer ' + key);
  assert.equal(app.requests[0].options.cache, 'no-store');
  assert.equal(app.requests[0].options.method ?? 'GET', 'GET');
  assert.deepEqual(app.redirects, ['/t/tabc123#k=' + encodeURIComponent(key)]);
  assert.equal(app.elements.get('signin-error').textContent, '');
  assert.equal(app.storage.size, 1, 'the access key is not persisted');

  const manual = browser(async () => { throw new Error('explicit team ID must bypass lookup'); });
  await manual.submit(key, ' tmanual123 ');
  assert.equal(manual.requests.length, 0);
  assert.deepEqual(manual.redirects, ['/t/tmanual123#k=' + encodeURIComponent(key)]);

  for (const status of [401, 403, 404, 429, 500, 503]) {
    const rejected = browser(async () => stateResponse(status, { error: key }));
    await rejected.submit(key);
    assert.equal(rejected.elements.get('signin-error').textContent, status === 401 ? 'Key not accepted' : 'Team not found');
    assert.equal(rejected.redirects.length, 0);
    assert.equal(rejected.elements.get('signin-submit').disabled, false);
  }
  for (const state of [null, {}, { team: null }, { team: {} }, { team: { id: 1 } }, { team: { id: '../viewer#' } }]) {
    const invalid = browser(async () => stateResponse(200, state));
    await invalid.submit(key);
    assert.equal(invalid.elements.get('signin-error').textContent, 'Team not found');
    assert.equal(invalid.redirects.length, 0);
  }
  for (const fetchResponse of [
    async () => { throw new Error(key); },
    async () => ({ status: 200, ok: true, json: async () => { throw new Error(key); } }),
  ]) {
    const failed = browser(fetchResponse);
    await failed.submit(key);
    assert.equal(failed.elements.get('signin-error').textContent, 'Team not found');
    assert.equal(failed.redirects.length, 0);
  }
  const empty = browser(async () => { throw new Error('empty key must not fetch'); });
  await empty.submit('   ');
  assert.equal(empty.elements.get('signin-error').textContent, 'Key not accepted');
  assert.equal(empty.requests.length, 0);
  const invalidTeam = browser(async () => { throw new Error('explicit team must not fetch'); });
  await invalidTeam.submit(key, '../viewer#');
  assert.equal(invalidTeam.elements.get('signin-error').textContent, 'Team not found');
  assert.equal(invalidTeam.redirects.length, 0);

  let finish;
  const pending = browser(() => new Promise(resolve => { finish = resolve; }));
  const firstSubmit = pending.submit(key);
  assert.equal(pending.elements.get('signin-submit').disabled, true);
  await pending.submit(key);
  assert.equal(pending.requests.length, 1, 'duplicate submissions do not race redirects');
  finish(stateResponse(200, { team: { id: 'tabc123' } }));
  await firstSubmit;
  assert.equal(pending.redirects.length, 1);

  const noStorage = browser(async () => stateResponse(200, { team: { id: 'tabc123' } }), true);
  noStorage.elements.get('theme-toggle').listeners.click();
  await noStorage.submit(key);
  assert.equal(noStorage.redirects.length, 1, 'unavailable theme storage does not block sign-in');

  // Exercise discovery against the actual offline Worker hub, not a changed API contract.
  const integrated = browser((route, options) => worker.default.fetch(new Request('https://agentmesh.zahid.win' + route, options), env));
  await integrated.submit(env.AGENT_A_TOKEN);
  assert.match(integrated.redirects[0], /^\/t\/t[a-z2-7]+#k=/);
  assert.ok(integrated.redirects[0].endsWith('#k=' + encodeURIComponent(env.AGENT_A_TOKEN)));
  console.log('Worker sign-in routing, nonce CSP, key discovery, redirects, theme and failures passed');
};
