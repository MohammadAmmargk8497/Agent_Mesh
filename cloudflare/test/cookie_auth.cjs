// Execute the real Worker boundary and Durable Objects against offline SQLite.
const assert = require('node:assert/strict');
const path = require('node:path');
const { load, context, root } = require('./runtime.cjs');

module.exports = async function testCookieAuth() {
  const worker = load(path.join(root, 'src/index.ts'));
  const env = {
    AGENT_A_TOKEN: 'cookie-agent-a-'.repeat(4), AGENT_B_TOKEN: 'cookie-agent-b-'.repeat(4),
    ADMIN_EXPORT_TOKEN: 'cookie-owner-'.repeat(4),
    ADMIN_EXPORT_LIMITER: { limit: async () => ({ success: true }) },
  };
  const legacy = context(), database = context();
  const room = new worker.Room(legacy.ctx, env);
  let hub = new worker.HubRoom(database.ctx, env);
  const forwarded = [];
  env.ROOM = { idFromName: name => name, get: () => ({ fetch: request => {
    forwarded.push({ target: 'room', path: new URL(request.url).pathname, headers: request.headers });
    return room.fetch(request);
  } }) };
  env.HUB = { idFromName: name => name, get: () => ({
    fetch: request => {
      forwarded.push({ target: 'hub', path: new URL(request.url).pathname, headers: request.headers });
      return hub.fetch(request);
    },
    teamTranscript: team => hub.teamTranscript(team), legacyTeamId: () => hub.legacyTeamId(),
    exportTokenIsMember: token => hub.exportTokenIsMember(token),
  }) };
  const origin = 'https://mesh.example';
  const cookie = key => 'agentmesh_session=' + key;
  async function request(route, { method = 'GET', headers = {}, body } = {}) {
    return worker.default.fetch(new Request(origin + route, {
      method, headers, ...(body === undefined ? {} : { body: typeof body === 'string' ? body : JSON.stringify(body) }),
    }), env);
  }
  async function value(route, options, status = 200) {
    const response = await request(route, options);
    assert.equal(response.status, status, route + ' response status');
    return response.json();
  }
  async function login(key) {
    return request('/auth/login', { method: 'POST', headers: { origin }, body: { key } });
  }
  try {
    assert.equal((await request('/api/state')).status, 401, 'fresh browser has no session');
    assert.equal((await request('/auth/login')).status, 405);
    for (const route of ['/auth/login', '/auth/logout', '/auth/unknown']) {
      for (const badOrigin of [undefined, 'https://attacker.example', 'https://mesh.example.attacker.example', 'http://mesh.example', 'null']) {
        const headers = badOrigin === undefined ? {} : { origin: badOrigin };
        assert.equal((await request(route, { method: 'POST', headers, body: { key: env.AGENT_A_TOKEN } })).status, 403,
          'every auth POST requires an exact same origin');
      }
    }
    assert.equal((await request('/auth/login', { method: 'POST', headers: { origin }, body: '{broken' })).status, 400);
    for (const key of ['', 'unknown', 'bad; cookie=value', 'bad\r\nheader', '"quoted"', 'bad\\key', 'bad,key', 'bad key', 'é', null, 7]) {
      const rejected = await login(key);
      assert.equal(rejected.status, 401);
      assert.equal(rejected.headers.get('set-cookie'), 'agentmesh_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0');
      assert.deepEqual(await rejected.json(), { error: 'unauthorized' });
    }
    const accepted = await login(env.AGENT_A_TOKEN);
    assert.equal(accepted.status, 200);
    assert.equal(accepted.headers.get('set-cookie'), cookie(env.AGENT_A_TOKEN) + '; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000');
    const signedIn = await accepted.json();
    assert.deepEqual(Object.keys(signedIn), ['team_id'], 'login never returns the access key');
    assert.equal(accepted.headers.get('cache-control'), 'no-store');
    const headers = { cookie: cookie(env.AGENT_A_TOKEN) };
    const state = await value('/api/state', { headers });
    assert.equal(state.team.id, signedIn.team_id);
    assert.equal(state.me.name, 'agent-a');
    // A fresh request (reload or a new tab) has no JavaScript credential state.
    assert.equal((await value('/api/state', { headers: { ...headers } })).team.id, state.team.id);
    assert.equal((await value('/v1/whoami', { headers })).agent, 'agent-a');
    assert.ok(forwarded.every(row => !row.headers.has('cookie')), 'cookies never reach either upstream');
    assert.ok(forwarded.filter(row => row.target === 'room').every(row => !row.headers.has('authorization')),
      'raw credentials never reach the legacy room');

    // WebSocket handshakes use GET and do not enforce CORS. SameSite alone
    // cannot stop a sibling origin from reading a cookie-authenticated stream.
    for (const badOrigin of [undefined, 'https://sibling.mesh.example', 'https://attacker.example']) {
      const before = forwarded.length;
      assert.equal((await request('/v1/connect', { headers: { ...headers, upgrade: 'websocket',
        ...(badOrigin ? { origin: badOrigin } : {}) } })).status, 403);
      assert.equal(forwarded.length, before, 'untrusted cookie upgrades stop before any upstream');
    }
    const actualRoom = env.ROOM;
    const upgrades = [];
    env.ROOM = { idFromName: name => name, get: () => ({ fetch: async request => {
      upgrades.push(request);
      return new Response(JSON.stringify({ forwarded: true }), { headers: { 'content-type': 'application/json' } });
    } }) };
    try {
      assert.equal((await value('/v1/connect', { headers: { ...headers, upgrade: 'websocket', origin } })).forwarded, true);
      assert.equal(upgrades[0].headers.get('x-agentmesh-agent'), 'agent-a');
      assert.equal(upgrades[0].headers.has('cookie'), false);
      assert.equal((await value('/v1/connect', { headers: { ...headers, upgrade: 'websocket',
        authorization: 'Bearer ' + env.AGENT_A_TOKEN } })).forwarded, true,
      'explicit Bearer WebSocket clients retain their existing Origin-independent contract');
    } finally { env.ROOM = actualRoom; }

    for (const authorization of ['', 'Bearer invalid', 'Basic invalid']) {
      for (const route of ['/api/state', '/v1/whoami']) {
        assert.equal((await request(route, { headers: { ...headers, authorization } })).status, 401,
          'an explicit invalid or empty header never falls back to the cookie');
      }
    }
    assert.equal((await value('/api/state', { headers: { ...headers, authorization: 'Bearer ' + env.AGENT_B_TOKEN } })).me.name, 'agent-b');
    assert.equal((await value('/v1/whoami', { headers: { ...headers, authorization: 'Bearer ' + env.AGENT_B_TOKEN } })).agent, 'agent-b');

    for (const malformed of [
      'agentmesh_session=', 'agentmesh_session="' + env.AGENT_A_TOKEN + '"',
      cookie(env.AGENT_A_TOKEN) + '; ' + cookie(env.AGENT_B_TOKEN),
      cookie(env.AGENT_A_TOKEN) + '; agentmesh_session=', 'agentmesh_session=invalid',
    ]) assert.equal((await request('/api/state', { headers: { cookie: malformed } })).status, 401);

    for (const method of ['POST', 'PUT', 'DELETE', 'PATCH']) {
      for (const route of ['/api/post', '/v1/messages']) {
        for (const badOrigin of [undefined, 'https://attacker.example', origin + ':443', origin + '/']) {
          assert.equal((await request(route, { method, headers: { ...headers, ...(badOrigin ? { origin: badOrigin } : {}) },
            body: { kind: 'update', text: 'blocked request', to: 'agent-b' } })).status, 403, 'cookie mutations require exact Origin');
        }
      }
    }
    assert.equal(database.db.prepare("SELECT COUNT(*) AS n FROM events WHERE kind='post'").get().n, 0,
      'blocked cookie requests cannot write');
    const post = await value('/api/post', { method: 'POST', headers: { ...headers, origin }, body: { kind: 'update', text: 'cookie post' } });
    assert.ok(post.event_id);
    await value('/v1/messages', { method: 'POST', headers: { ...headers, origin }, body: { to: 'agent-b', text: 'cookie message' } }, 201);
    await value('/api/post', { method: 'POST', headers: { ...headers, authorization: 'Bearer ' + env.AGENT_A_TOKEN },
      body: { kind: 'update', text: 'explicit bearer keeps its existing non-browser contract' } });
    assert.equal((await request('/admin/export/tables', { headers: { cookie: cookie(env.ADMIN_EXPORT_TOKEN) } })).status, 401,
      'a session cookie never authorizes owner-admin routes');
    assert.equal((await request('/admin/export/tables', { headers })).status, 401);
    const logout = await request('/auth/logout', { method: 'POST', headers: { ...headers, origin } });
    assert.equal(logout.status, 200);
    assert.equal(logout.headers.get('set-cookie'), 'agentmesh_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0');
    assert.deepEqual(await logout.json(), { ok: true });
    assert.equal((await request('/api/state')).status, 401, 'a browser applying the cleared cookie is signed out');

    // Generated members use the same hub team-state validation as /api/state.
    const created = await value('/api/teams', { method: 'POST', body: { team_name: 'Cookie team', member_name: 'owner' } });
    const member = await value('/api/join', { method: 'POST', body: { code: created.invite_url, member_name: 'browser member' } });
    const memberLogin = await login(member.token);
    assert.equal(memberLogin.status, 200);
    assert.deepEqual(await memberLogin.json(), { team_id: created.team_id });
    const memberHeaders = { cookie: cookie(member.token) };
    assert.equal((await value('/api/state', { headers: memberHeaders })).me.name, 'browser member');
    await value('/api/members/remove', { method: 'POST', headers: { authorization: 'Bearer ' + created.token }, body: { member: member.member_id } });
    assert.equal((await request('/api/state', { headers: memberHeaders })).status, 401);
    assert.equal((await request('/v1/whoami', { headers: memberHeaders })).status, 401, 'revoked browser sessions cannot use the legacy gate');
    assert.equal((await login(member.token)).status, 401);

    // Rotation can retain a legacy key in the environment, but not in hub state.
    const oldKey = env.AGENT_A_TOKEN;
    const newKey = 'cookie-rotated-agent-a-'.repeat(3);
    env.AGENT_A_TOKEN = newKey + ',' + oldKey;
    hub = new worker.HubRoom(database.ctx, env);
    assert.equal((await request('/v1/whoami', { headers: { authorization: 'Bearer ' + oldKey } })).status, 200,
      'explicit legacy Bearer behavior remains unchanged');
    assert.equal((await request('/api/state', { headers: { cookie: cookie(oldKey) } })).status, 401);
    assert.equal((await request('/v1/whoami', { headers: { cookie: cookie(oldKey) } })).status, 401,
      'cookie authentication also checks current hub state before native legacy routes');
    assert.equal((await login(oldKey)).status, 401);
    assert.equal((await login(newKey)).status, 200);
    assert.equal((await value('/v1/whoami', { headers: { cookie: cookie(newKey) } })).agent, 'agent-a');
  } finally { legacy.db.close(); database.db.close(); }
};

if (require.main === module) module.exports().then(() => console.log('Worker cookie login, isolation, CSRF, logout, revocation and rotation passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
