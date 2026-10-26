// Unified dashboard routing and default-team-only legacy adapter contracts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { load, context, root } = require('./runtime.cjs');

module.exports = async function testUnifiedRoutes() {
  const worker = load(path.join(root, 'src/index.ts'));
  const env = { AGENT_A_TOKEN: 'adapter-a'.repeat(5), AGENT_B_TOKEN: 'adapter-b'.repeat(5), VIEWER_TOKEN: 'adapter-v'.repeat(5) };
  const legacy = context(), database = context();
  const room = new worker.Room(legacy.ctx, env), hub = new worker.HubRoom(database.ctx, env);
  env.ROOM = { idFromName: name => name, get: () => ({ fetch: request => room.fetch(request) }) };
  env.HUB = { idFromName: name => name, get: () => ({ fetch: request => hub.fetch(request) }) };
  async function request(route, token, method = 'GET', data, host = 'mesh.example', extra = {}) {
    return worker.default.fetch(new Request('https://' + host + route, {
      method, headers: { ...(token ? { authorization: 'Bearer ' + token } : {}), ...extra },
      ...(data === undefined ? {} : { body: JSON.stringify(data) }),
    }), env);
  }
  async function value(route, token = env.AGENT_A_TOKEN, method = 'GET', data, status = 200) {
    const response = await request(route, token, method, data);
    assert.equal(response.status, status, route + ': ' + await response.clone().text());
    return response.json();
  }
  try {
    const initial = await value('/api/state');
    const team = initial.team.id;
    const invite = await value('/api/invite', env.AGENT_A_TOKEN, 'POST', {});
    const joined = await value('/api/join', null, 'POST', { code: invite.invite_url, member_name: 'Observer' });
    const other = await value('/api/teams', null, 'POST', { team_name: 'default', member_name: 'agent-a' });
    const sent = await value('/v1/messages', env.AGENT_A_TOKEN, 'POST', { to: 'agent-b', text: 'default-team-only' }, 201);
    const patch = await value('/v1/patches', env.AGENT_A_TOKEN, 'POST', { title: 'Private patch', text: 'private diff' }, 201);
    const privateTask = await value('/v1/tasks', env.AGENT_B_TOKEN, 'POST', {
      title: 'Agent-b task', description: 'Full team transcript coverage', assignee: 'agent-b',
    }, 201);
    const privateMessage = '00000000-0000-4000-8000-000000000001';
    legacy.db.prepare("INSERT INTO messages (id,sender,recipient,text,created_at) VALUES (?,'agent-b','agent-b','Team transcript note',?)")
      .run(privateMessage, Date.now());
    const agentSnapshot = await value('/v1/dashboard/state');
    assert.ok(!agentSnapshot.tasks.some(row => row.id === privateTask.id));
    assert.ok(!agentSnapshot.messages.some(row => row.id === privateMessage));
    const route = '/api/legacy/transcript?team=' + team;
    // A generated member key, not just the environment agent token, can read the default team's panels.
    const snapshot = await value(route, joined.token);
    // A cold boundary can also resolve a generated member without any preceding
    // seeded-agent request. The binding must then stay cached on that environment.
    const coldEnv = { ...env };
    const coldRead = () => worker.default.fetch(new Request('https://mesh.example' + route, {
      headers: { authorization: 'Bearer ' + joined.token },
    }), coldEnv);
    assert.equal((await coldRead()).status, 200);
    assert.ok(snapshot.messages.some(row => row.id === sent.id));
    assert.ok(snapshot.patches.some(row => row.id === patch.id));
    assert.ok(snapshot.tasks.some(row => row.id === privateTask.id), 'member adapter retains the full team task snapshot');
    assert.ok(snapshot.messages.some(row => row.id === privateMessage), 'member adapter retains the full team transcript');
    assert.ok(snapshot.audit.length);
    assert.equal((await value('/api/legacy/patches?team=' + team, joined.token)).patches[0].id, patch.id);
    assert.equal((await value('/api/legacy/patches/' + patch.id + '?team=' + team, joined.token)).patch.text, 'private diff');
    assert.equal((await request(route)).status, 401);
    assert.equal((await request(route, env.VIEWER_TOKEN)).status, 401);
    assert.equal((await request('/api/legacy/transcript', joined.token)).status, 403);
    assert.equal((await request('/api/legacy/transcript?team=' + other.team_id, joined.token)).status, 403);
    // Even a team named default, with a member named agent-a, cannot enter the seeded team's room.
    for (const suffix of ['/transcript', '/patches', '/patches/' + patch.id]) {
      assert.equal((await request('/api/legacy' + suffix + '?team=' + other.team_id, other.token)).status, 403);
      assert.equal((await request('/api/legacy' + suffix + '?team=' + team, other.token, 'GET', undefined,
        'mesh.example', { 'x-agentmesh-agent': 'viewer' })).status, 403);
    }
    assert.equal((await request(route, joined.token, 'POST', {})).status, 405);
    assert.equal((await request('/api/legacy/tasks?team=' + team, joined.token)).status, 404);
    assert.equal((await request('/v1/transcript', env.AGENT_A_TOKEN)).status, 403);
    for (const route of ['/v1/transcript', '/v1/whoami', '/v1/dashboard/state', '/api/state', '/api/legacy/transcript?team=' + team]) {
      assert.equal((await request(route, env.VIEWER_TOKEN)).status, 401, 'former viewer key rejected: ' + route);
    }
    assert.equal((await request('/v1/messages', env.VIEWER_TOKEN, 'POST', { to: 'agent-b', text: 'not accepted' })).status, 401);
    for (const token of [env.AGENT_A_TOKEN, env.AGENT_B_TOKEN, env.VIEWER_TOKEN, joined.token]) {
      const expected = token === env.AGENT_A_TOKEN || token === env.AGENT_B_TOKEN ? 404 : 401;
      assert.equal((await request('/internal/transcript', token, 'GET', undefined,
        'mesh.example', { 'x-agentmesh-agent': 'legacy-adapter' })).status, expected,
        'internal snapshot cannot be reached by spoofing a context header');
    }
    assert.equal((await room.fetch(new Request('https://mesh.example/v1/transcript', {
      headers: { 'x-agentmesh-agent': 'viewer' },
    }))).status, 401, 'the room does not recognize a viewer identity');
    // Once bound, adapter refreshes must not authenticate as the seeded agent and fake its presence.
    database.db.prepare('UPDATE members SET last_seen=1 WHERE name=? AND team_id=?').run('agent-a', team);
    await value(route, joined.token);
    assert.equal((await coldRead()).status, 200);
    assert.equal(database.db.prepare('SELECT last_seen FROM members WHERE name=? AND team_id=?').get('agent-a', team).last_seen, 1);
    const dashboard = fs.readFileSync(path.join(root, 'src/dashboard.html'), 'utf8');
    for (const [host, route, template] of [
      ['mesh.example', '/t/' + team, dashboard], ['agentmesh.zahid.win', '/t/' + team, dashboard],
      ['agentmesh.zahid.win', '/', dashboard],
    ]) {
      const response = await request(route, null, 'GET', undefined, host);
      assert.equal(response.status, 200);
      const html = await response.text();
      const nonce = /script-src 'nonce-([^']+)'/.exec(response.headers.get('content-security-policy'))[1];
      assert.equal(html, template.replaceAll('__AGENTMESH_NONCE__', nonce));
      assert.ok(!/OpenHuddle/i.test(html));
      for (const token of [env.AGENT_A_TOKEN, env.AGENT_B_TOKEN, joined.token, other.token]) assert.ok(!html.includes(token));
      const head = await request(route, null, 'HEAD', undefined, host);
      assert.equal(head.status, 200);
      assert.equal(await head.text(), '');
    }
    for (const route of ['/viewer', '/index.html']) {
      for (const method of ['GET', 'HEAD']) {
        const response = await request(route, null, method, undefined, 'agentmesh.zahid.win');
        assert.equal(response.status, 302);
        assert.equal(response.headers.get('location'), '/');
        assert.equal(await response.text(), '');
      }
    }
    await value('/api/members/remove', env.AGENT_A_TOKEN, 'POST', { member: joined.member_id });
    assert.equal((await request(route, joined.token)).status, 401, 'revocation takes effect for adapters too');
  } finally { legacy.db.close(); database.db.close(); }
};

if (require.main === module) module.exports().then(() => console.log('Unified Worker routes and default-team adapter isolation passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
