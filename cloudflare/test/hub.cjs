const assert = require('node:assert/strict');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { load, context, root } = require('./runtime.cjs');
const testSignin = require('./signin.cjs');
const worker = load(path.join(root, 'src/index.ts'));
const { DoSqlite } = load(path.join(root, 'src/openhuddle/do-sqlite.ts'));
// Direct-row changes differ from billing writes and exclude trigger side effects.
const metadataDb = context();
try {
  const rawSql = metadataDb.ctx.storage.sql;
  let billedDeletion = 0;
  metadataDb.ctx.storage.sql = { exec(query, ...params) {
    const cursor = rawSql.exec(query, ...params);
    if (query.startsWith('DELETE FROM probe')) billedDeletion = cursor.rowsWritten;
    return cursor;
  } };
  const adapter = new DoSqlite(metadataDb.ctx.storage);
  adapter.exec('CREATE TABLE probe (id INTEGER PRIMARY KEY, path TEXT); CREATE INDEX probe_path ON probe(path); CREATE TABLE audit_probe (path TEXT);');
  adapter.exec("CREATE TRIGGER probe_delete AFTER DELETE ON probe BEGIN INSERT INTO audit_probe(path) VALUES (old.path); END;");
  const inserted = adapter.prepare('INSERT INTO probe(path) VALUES (?) RETURNING id').run('src/file.ts');
  assert.equal(inserted.changes, 1);
  assert.equal(inserted.lastInsertRowid, 1);
  assert.equal(adapter.prepare('DELETE FROM probe WHERE path = ?').run('src/file.ts').changes, 1);
  assert.ok(billedDeletion > 1, 'fake billing includes index/trigger writes');
  assert.equal(adapter.prepare('SELECT COUNT(*) AS n FROM audit_probe').get().n, 1, 'trigger ran without inflating direct-row changes');
  assert.equal(adapter.prepare('DELETE FROM probe WHERE path = ?').run('src/file.ts').changes, 0);
} finally { metadataDb.db.close(); }
const sha = text => createHash('sha256').update(text).digest('hex');
const env = { AGENT_A_TOKEN: 'a'.repeat(32), AGENT_B_TOKEN: 'b'.repeat(32), VIEWER_TOKEN: 'v'.repeat(32) };
const legacy = context(), database = context();
const room = new worker.Room(legacy.ctx, env);
let hub = new worker.HubRoom(database.ctx, env);
const limiterKeys = [];
env.LIMITER = { limit: async ({ key }) => { limiterKeys.push(key); return { success: true }; } };
env.ROOM = { idFromName: name => name, get: () => ({ fetch: request => room.fetch(request) }) };
env.HUB = { idFromName: name => { assert.equal(name, 'openhuddle-hub'); return name; }, get: () => ({ fetch: request => hub.fetch(request) }) };
async function response(method, route, body, token, extras = {}) {
  const headers = { 'cf-connecting-ip': '127.0.0.1', ...extras };
  if (token) headers.authorization = 'Bearer ' + token;
  if (body !== undefined) headers['content-type'] = 'application/json';
  return worker.default.fetch(new Request('https://mesh.example' + route, {
    method, headers, ...(body !== undefined ? { body: typeof body === 'string' ? body : JSON.stringify(body) } : {}),
  }), env);
}
async function call(method, route, body, token, status = 200) {
  const res = await response(method, route, body, token);
  const value = await res.json();
  assert.equal(res.status, status, route + ': ' + JSON.stringify(value));
  return value;
}
const get = (route, token = env.AGENT_A_TOKEN, status = 200) => call('GET', route, undefined, token, status);
const post = (route, body, token, status = 200) => call('POST', route, body, token, status);
async function drain(reader) { let text = ''; while (true) { const r = await reader.read(); if (r.done) return text; text += new TextDecoder().decode(r.value); } }
(async () => {
  await require('./retention.cjs')();
  await require('./history.cjs')();
  await testSignin(worker, env);
  await require('./unified_routes.cjs')();
  let state = await get('/api/state');
  assert.equal(state.team.name, 'default');
  assert.equal(state.me.name, 'agent-a');
  assert.equal(state.me.role, 'owner');
  assert.equal(state.members.length, 2);
  assert.equal((await get('/api/state', env.AGENT_B_TOKEN)).me.name, 'agent-b');
  const seededId = state.team.id;
  assert.match(seededId, /^t[a-z2-7]+$/);
  assert.equal(database.db.prepare('SELECT token_hash FROM members WHERE name=?').get('agent-a').token_hash, sha(env.AGENT_A_TOKEN));
  assert.equal(state.team.settings.assign_mode, 'hybrid');
  assert.equal(state.team.settings.claim_mode, 'ask');
  assert.equal(state.team.settings.claim_ttl_min, 30);
  const seeded = database.db.prepare('SELECT * FROM teams WHERE id=?').get(seededId);
  assert.equal(seeded.invite_expires_at - seeded.created_at, 7 * 86400000);
  hub = new worker.HubRoom(database.ctx, env);
  assert.equal((await get('/api/state')).members.length, 2);
  const oldA = env.AGENT_A_TOKEN;
  const newA = 'new-a'.repeat(8);
  env.AGENT_A_TOKEN = newA + ',' + oldA;
  hub = new worker.HubRoom(database.ctx, env);
  state = await get('/api/state', newA);
  assert.equal(state.me.name, 'agent-a');
  assert.equal(state.team.id, seededId);
  assert.equal(state.members.length, 2);
  await get('/api/state', oldA, 401);
  assert.equal((await get('/v1/whoami', oldA)).agent, 'agent-a');
  assert.equal((await get('/v1/whoami', newA)).agent, 'agent-a');
  const owner = await post('/api/teams', { team_name: 'Test team', member_name: 'owner' });
  assert.ok(owner.token && owner.invite_url && owner.dashboard_url);
  assert.ok(owner.dashboard_url.startsWith('https://mesh.example/t/'));
  const member = await post('/api/join', { code: owner.invite_url, member_name: 'member' });
  const member2 = await post('/api/join', { code: owner.invite_url, member_name: 'member' });
  assert.equal(member2.member_name, 'member-2', 'invite is reusable');
  await post('/api/invite', { rotate: true }, member.token, 403);
  await post('/api/team/delete', {}, member.token, 403);
  await post('/api/members/remove', { member: owner.member_id }, member.token, 403);
  const invite = await post('/api/invite', { rotate: true }, owner.token);
  await post('/api/join', { code: owner.invite_url, member_name: 'expired' }, undefined, 403);
  assert.notEqual(invite.invite_url, owner.invite_url);
  await post('/api/post', { kind: 'update', text: 'Hello team' }, member.token);
  const plan = await post('/api/plan', { tasks: [{ title: 'Implement teams' }] }, owner.token);
  assert.ok(plan.tasks.length);
  const claimed = await post('/api/claims', { paths: ['src/new.ts'] }, member.token);
  assert.deepEqual(claimed.claimed, ['src/new.ts']);
  assert.equal((await post('/api/claims', { action: 'release', paths: ['src/new.ts'] }, member.token)).released, 1, 'release counts claims, not billed index writes');
  await post('/api/claims', { paths: ['src/new.ts'] }, member.token);
  const before = Date.now();
  const activeClaim = (await get('/api/state', member.token)).claims[0];
  assert.ok(activeClaim.expires_at >= before + 30 * 60000 - 1000);
  const talk = await post('/api/huddle/start', { topic: 'Design teams' }, owner.token);
  assert.ok(talk.huddle.id);
  await post('/api/huddle/post', { id: talk.huddle.id, text: 'Use existing upstream domain logic.' }, member.token);
  await post('/api/huddle/close', { id: talk.huddle.id, decision: 'Agreed' }, owner.token);
  // SSE clamps a future cursor and still delivers new events; cancellation removes all bookkeeping.
  const stream = await response('GET', '/api/stream?since=999999999', undefined, member.token);
  assert.equal(stream.headers.get('content-type'), 'text/event-stream; charset=utf-8');
  const reader = stream.body.getReader();
  assert.match(new TextDecoder().decode((await reader.read()).value), /connected/);
  const event = await post('/api/post', { kind: 'update', text: 'New event after future cursor' }, owner.token);
  const chunk = new TextDecoder().decode((await reader.read()).value);
  assert.match(chunk, /New event after future cursor/);
  assert.match(chunk, new RegExp('id: ' + event.event_id));
  await reader.cancel();
  assert.equal(hub.hub.streams.size, 0);
  // Last-Event-ID wins over since and replay stays ordered.
  const replay = await response('GET', '/api/stream?since=999999999', undefined, member.token, { 'last-event-id': String(event.event_id - 1) });
  const replayReader = replay.body.getReader();
  await replayReader.read();
  assert.match(new TextDecoder().decode((await replayReader.read()).value), /New event after future cursor/);
  await replayReader.cancel();
  // A fourth stream replaces the oldest per-member feed.
  const streams = [];
  for (let i = 0; i < 4; i++) streams.push(await response('GET', '/api/stream?since=999999999', undefined, member.token));
  assert.equal(hub.hub.streams.get(member.member_id), 3);
  await drain(streams[0].body.getReader());
  for (const s of streams.slice(1)) await s.body.cancel();
  assert.equal(hub.hub.streams.size, 0);
  // Revocation emits the upstream member_removed event and closes that member's live feed.
  const revoked = await response('GET', '/api/stream?since=999999999', undefined, member.token);
  const revokedReader = revoked.body.getReader();
  await revokedReader.read();
  await post('/api/members/remove', { member: member.member_id }, owner.token);
  assert.match(await drain(revokedReader), /member_removed/);
  await get('/api/state', member.token, 401);
  assert.equal((await get('/api/state', owner.token)).claims.length, 0);
  await post('/api/leave', {}, member2.token);
  await get('/api/state', member2.token, 401);
  // Queue bytes are bounded even when a reader stops consuming.
  const slow = await response('GET', '/api/stream?since=999999999', undefined, owner.token);
  for (let i = 0; i < 140; i++) await post('/api/post', { kind: 'update', text: 'x'.repeat(7999) }, owner.token);
  assert.equal(hub.hub.streams.size, 0, 'unread SSE is closed at the upstream byte budget');
  await drain(slow.body.getReader());
  await post('/api/team/delete', {}, owner.token);
  await get('/api/state', owner.token, 401);
  assert.equal(database.db.prepare('SELECT COUNT(*) AS n FROM teams WHERE id=?').get(owner.team_id).n, 0);
  // Reject malformed and streaming oversized input without retaining arbitrary bytes.
  await post('/api/teams', '[]', undefined, 400);
  await post('/api/teams', '{', undefined, 400);
  let canceled = false;
  const oversized = await worker.default.fetch(new Request('https://mesh.example/api/post', {
    method: 'POST', headers: { authorization: 'Bearer ' + newA }, duplex: 'half',
    body: new ReadableStream({ start(controller) {
      controller.enqueue(new Uint8Array(262144)); controller.enqueue(new Uint8Array(1));
    }, cancel() { canceled = true; } }),
  }), env);
  assert.equal(oversized.status, 413); assert.equal(canceled, true);
  // Pages are public, fully inlined and nonce-protected; the same IP limiter applies.
  for (const route of ['/t/' + seededId, '/join/' + seededId + '.test_invite']) {
    const page = await response('GET', route);
    assert.equal(page.status, 200);
    const html = await page.text();
    const csp = page.headers.get('content-security-policy');
    const nonce = /script-src 'nonce-([^']+)'/.exec(csp)[1];
    assert.ok(html.includes('nonce="' + nonce + '"'));
    assert.ok(!html.includes('__AGENTMESH_NONCE__'));
    assert.ok(!html.includes('{{HUB_URL}}'));
    assert.ok(!/\binnerHTML\b/.test(html));
    assert.ok(!/(?:src|href)="\/static\//.test(html));
    assert.equal((await response('HEAD', route)).status, 200);
  }
  assert.match(await (await response('GET', '/robots.txt')).text(), /Disallow: \/t\//);
  assert.ok(limiterKeys.includes('ip:127.0.0.1'));
  env.LIMITER = { limit: async () => ({ success: false }) };
  assert.equal((await response('GET', '/t/' + seededId)).status, 429);
  assert.equal((await response('GET', '/api/state', undefined, newA)).status, 429);
  console.log('Worker hub seed/reconcile, lifecycle, upstream routes, SSE, pages and legacy isolation passed');
})().catch(err => { console.error(err); process.exitCode = 1; }).finally(() => { legacy.db.close(); database.db.close(); });
