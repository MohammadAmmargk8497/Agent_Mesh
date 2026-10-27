// No test/runtime dependency is added: use the existing TypeScript compiler and
// Node 22's built-in SQLite to execute the real Worker and Durable Object code.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { DatabaseSync } = require('node:sqlite');
const { webcrypto, timingSafeEqual } = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const ts = require(path.join(root, 'cloudflare/node_modules/typescript'));
webcrypto.subtle.timingSafeEqual = (a, b) => timingSafeEqual(Buffer.from(a), Buffer.from(b));

function load(name, imports) {
  const filename = path.join(root, 'cloudflare/src', name + '.ts');
  const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const module = { exports: {} };
  const context = { module, exports: module.exports, require: id => {
    assert.ok(Object.hasOwn(imports, id), 'unexpected import: ' + id);
    return imports[id];
  }, crypto: webcrypto, Request, Response, Headers, URL, TextEncoder, TextDecoder,
  Uint8Array, ArrayBuffer, btoa, console, setTimeout, clearTimeout };
  vm.runInNewContext(source, context, { filename });
  return module.exports;
}

const coordination = load('coordination', {});
const exportHelpers = load('openhuddle/export', {});
const recovery = load('openhuddle/admin-recovery', { '../coordination': coordination, './export': exportHelpers });
const worker = load('index', {
  'cloudflare:workers': { DurableObject: class {
    constructor(ctx, env) { this.ctx = ctx; this.env = env; }
  } },
  './dashboard.html': { default: '' }, './signin.html': { default: '' }, './hub_dashboard.html': { default: '' },
  './hub_join.html': { default: '' }, './openhuddle/hub-room': { HubRoom: class {} }, './coordination': coordination,
  './openhuddle/export': exportHelpers, './openhuddle/admin-recovery': recovery,
});
const db = new DatabaseSync(':memory:');
const sql = { exec(query, ...args) {
  let rows = [];
  if (!args.length && query.trim().split(';').filter(Boolean).length > 1) db.exec(query);
  else {
    const statement = db.prepare(query);
    if (statement.columns().length) rows = statement.all(...args);
    else statement.run(...args);
  }
  return { toArray: () => rows, one: () => {
    assert.equal(rows.length, 1); return rows[0];
  }, rowsWritten: Number(db.prepare('SELECT changes() AS n').get().n) };
} };
const storage = { sql, getAlarm: async () => 1, setAlarm: async () => {}, transactionSync(callback) {
  db.exec('BEGIN');
  try { const result = callback(); db.exec('COMMIT'); return result; }
  catch (error) { db.exec('ROLLBACK'); throw error; }
} };
const ctx = { storage, blockConcurrencyWhile: callback => callback(), getWebSockets: () => [] };
const env = { AGENT_A_TOKEN: 'a'.repeat(32), AGENT_B_TOKEN: 'b'.repeat(32), VIEWER_TOKEN: 'v'.repeat(32) };
const room = new worker.Room(ctx, env);
env.ROOM = { idFromName: () => 'room', get: () => ({ fetch: request => room.fetch(request) }) };

async function request(method, route, body, role = 'a', status = 200) {
  const headers = {};
  if (role) headers.authorization = 'Bearer ' + env[{ a: 'AGENT_A_TOKEN', b: 'AGENT_B_TOKEN', viewer: 'VIEWER_TOKEN' }[role]];
  if (body !== undefined) headers['content-type'] = 'application/json';
  const response = await worker.default.fetch(new Request('https://mesh.example' + route, {
    method, headers, ...(body !== undefined ? { body: typeof body === 'string' ? body : JSON.stringify(body) } : {}),
  }), env);
  const value = await response.json();
  assert.equal(response.status, status, method + ' ' + route + ': ' + JSON.stringify(value));
  return value;
}
const get = (route, role = 'a', status = 200) => request('GET', route, undefined, role, status);
const post = (route, body = {}, role = 'a', status = 200) => request('POST', route, body, role, status);
const createTask = async (role = 'a', assignee = 'agent-b', extras = {}) => (await post('/v1/tasks', {
  title: 'Task', description: 'Description', assignee, ...extras,
}, role, 201)).id;

(async () => {
  const huddle = (await post('/v1/huddles', { kind: 'design', topic: 'API' }, 'a', 201)).id;
  const base = '/v1/huddles/' + huddle;
  assert.deepEqual((await get('/v1/dashboard/state')).huddles[0].waiting_on, ['agent-b']);
  assert.deepEqual((await get(base)).huddle.waiting_on, ['agent-a', 'agent-b']);
  const legacyTranscriptHuddle = (await (await room.fetch(new Request('https://mesh.example/internal/transcript', {
    headers: { 'x-agentmesh-agent': 'legacy-adapter' },
  }))).json()).huddles[0];
  assert.deepEqual(legacyTranscriptHuddle.waiting_on, ['agent-a', 'agent-b']);
  assert.deepEqual(legacyTranscriptHuddle.dashboard_waiting_on, ['agent-b']);
  const idea = (await post(base + '/posts', { kind: 'idea', text: 'A typed API' }, 'a', 201)).id;
  const actions = ['take', 'accept', 'decline', 'start', 'release', 'finish'];
  const task = await createTask(), taskBase = '/v1/dashboard/tasks/' + task;
  for (const route of [base + '/comments', base + '/votes', ...actions.map(a => taskBase + '/' + a)]) {
    await post(route, {}, null, 401);
    await post(route, {}, 'viewer', 401);
    await post(route, '[]', 'b', 400);
    await post(route, '{', 'b', 400);
    await post(route, 'x'.repeat(256 * 1024 + 1), 'b', 413);
  }
  await get('/v1/dashboard/state', null, 401);
  await get('/v1/dashboard/state', 'viewer', 401);
  await get('/v1/transcript', 'viewer', 401);
  await get('/v1/transcript', 'a', 403);
  await get('/v1/whoami', 'viewer', 401);
  await get('/v1/dashboard/state?after=no', 'a', 400);
  await get('/v1/dashboard/state?after=9007199254740992', 'a', 400);
  await get(taskBase + '/take', 'a', 405);
  let canceled = false;
  const streamed = await worker.default.fetch(new Request('https://mesh.example' + base + '/comments', {
    method: 'POST', headers: { authorization: 'Bearer ' + env.AGENT_B_TOKEN }, duplex: 'half',
    body: new ReadableStream({ start(controller) {
      controller.enqueue(new Uint8Array(256 * 1024));
      controller.enqueue(new Uint8Array(1));
    }, cancel() { canceled = true; } }),
  }), env);
  assert.equal(streamed.status, 413, 'unknown-length bodies are capped while reading');
  assert.equal(canceled, true);
  await post(base + '/comments', {}, 'b', 400);
  await post(base + '/comments', { text: 'x'.repeat(8001) }, 'b', 400);
  await post(base + '/comments', { text: 'a', ref_post_id: 'invalid' }, 'b', 400);
  const comment = (await post(base + '/comments', { text: 'Hello\u0000 world', ref_post_id: idea }, 'b', 201)).id;
  await post(base + '/votes', { ref_post_id: idea, vote: 'up' }, 'a', 403);
  await post(base + '/votes', { ref_post_id: comment, vote: 'up' }, 'a', 400);
  await post(base + '/votes', { ref_post_id: idea, vote: 'sideways' }, 'b', 400);
  await post(base + '/votes', { ref_post_id: idea, vote: 'up', note: 'x'.repeat(501) }, 'b', 400);
  await post(base + '/votes', { ref_post_id: idea, vote: 'up', note: 'Agree\u0000!' }, 'b');
  await post(base + '/votes', { ref_post_id: idea, vote: 'down', note: 'Changed opinion' }, 'b');
  const snapshot = await get('/v1/dashboard/state');
  assert.equal(snapshot.me, 'agent-a');
  assert.equal(snapshot.huddles[0].posts[1].text, 'Hello world');
  assert.equal(snapshot.huddles[0].votes.length, 1);
  assert.equal(snapshot.huddles[0].votes[0].vote, 'down');
  const voteNews = snapshot.news.filter(n => n.kind === 'huddle_voted');
  assert.match(voteNews[0].summary, /Backed idea.*Agree!/);
  assert.match(voteNews[1].summary, /Pushed back on idea.*Changed opinion/);
  assert.ok(snapshot.news.some(n => n.kind === 'huddle_commented' && n.summary.includes('Hello world')));
  assert.deepEqual(snapshot.huddles[0].waiting_on, []);
  assert.equal(snapshot.tasks[0].dashboard_status, 'proposed');
  assert.ok(!Object.hasOwn((await get('/v1/tasks/' + task)).task, 'dashboard_status'));
  assert.ok(!Object.hasOwn((await get(base)).huddle, 'votes'));
  assert.equal(snapshot.member_settings.length, 2);
  await post('/v1/settings', { auto_respond: false });
  assert.equal((await get('/v1/dashboard/state')).member_settings[0].auto_respond, false);

  await post(taskBase + '/accept', {}, 'a', 403);
  await post(taskBase + '/start', {}, 'b', 409);
  assert.deepEqual(await post(taskBase + '/accept', {}, 'b'), { id: task, status: 'open', dashboard_status: 'assigned' });
  await post(taskBase + '/accept', {}, 'b', 409);
  await post(taskBase + '/take', {}, 'a', 409);
  await post(taskBase + '/finish', {}, 'b', 409);
  await post(taskBase + '/release', {}, 'a', 403);
  assert.equal((await post(taskBase + '/start', {}, 'b')).status, 'in_progress');
  await post('/v1/claims', { path: 'src/task.ts', task_id: task }, 'b', 201);
  assert.equal((await post(taskBase + '/finish', { summary: 'Done\u0000!' }, 'b')).status, 'done');
  assert.equal((await get('/v1/tasks/' + task)).task.result, 'Done!');
  assert.equal((await get('/v1/claims')).claims.length, 0);
  await post(taskBase + '/finish', {}, 'b', 409);
  await post(taskBase + '/release', {}, 'b', 409);

  const declined = await createTask(), declinedBase = '/v1/dashboard/tasks/' + declined;
  assert.equal((await post(declinedBase + '/decline', {}, 'b')).dashboard_status, 'open');
  await post(declinedBase + '/decline', {}, 'b', 409);
  assert.equal((await post(declinedBase + '/take', {})).dashboard_status, 'assigned');
  await post('/v1/claims', { path: 'src/declined.ts', task_id: declined }, 'a', 201);
  assert.equal((await post(declinedBase + '/release', {})).dashboard_status, 'open');
  assert.equal((await get('/v1/claims')).claims.length, 0);
  const dependency = await createTask(), blocked = await createTask('a', 'agent-b', { depends_on: [dependency] });
  await post('/v1/dashboard/tasks/' + blocked + '/accept', {}, 'b');
  await post('/v1/dashboard/tasks/' + blocked + '/start', {}, 'b', 409);
  const reopened = await createTask(), reopenedBase = '/v1/dashboard/tasks/' + reopened;
  await post(reopenedBase + '/accept', {}, 'b');
  await post('/v1/tasks/' + reopened + '/claim', {}, 'b');
  await post('/v1/tasks/' + reopened + '/complete', { summary: 'Ready' }, 'b');
  await post('/v1/tasks/' + reopened + '/review', { decision: 'reopen' });
  assert.equal((await get('/v1/dashboard/state')).tasks.find(t => t.id === reopened).dashboard_status, 'proposed');
  assert.equal((await post(reopenedBase + '/accept', {}, 'b')).dashboard_status, 'assigned');

  const privateTask = await createTask('b', 'agent-b');
  const upperTask = await createTask(), upperBase = '/v1/dashboard/tasks/' + upperTask;
  assert.equal((await post(upperBase + '/ACCEPT', {}, 'b')).dashboard_status, 'assigned');
  assert.equal((await post(upperBase + '/START', {}, 'b')).status, 'in_progress');
  assert.equal((await post(upperBase + '/FINISH', {}, 'b')).status, 'done');
  for (const action of actions) await post('/v1/dashboard/tasks/' + privateTask + '/' + action, {}, 'a', 404);
  const releasedPrivate = await createTask('b', 'agent-b'), releasedBase = '/v1/dashboard/tasks/' + releasedPrivate;
  await post(releasedBase + '/accept', {}, 'b');
  await post(releasedBase + '/release', {}, 'b');
  assert.equal((await get('/v1/dashboard/state')).tasks.find(t => t.id === releasedPrivate).dashboard_status, 'open');
  for (const action of actions.filter(a => a !== 'take')) await post(releasedBase + '/' + action, {}, 'a', 404);
  assert.equal((await post(releasedBase + '/take', {})).dashboard_status, 'assigned');
  assert.equal((await get('/v1/tasks/' + releasedPrivate)).task.assignee, 'agent-a');
  await post('/v1/messages', { to: 'agent-a', text: 'shared' }, 'b', 201);
  const now = Date.now();
  sql.exec('INSERT INTO messages(id,sender,recipient,text,created_at) VALUES (?,?,?,?,?)', 'private', 'agent-b', 'agent-b', 'secret', now);
  sql.exec('INSERT INTO audit(actor,action,ref,created_at) VALUES (?,?,?,?)', 'agent-b', 'private', privateTask, now);
  sql.exec('INSERT INTO questions(id,sender,recipient,text,created_at) VALUES (?,?,?,?,?)', 'private', 'agent-b', 'agent-b', 'secret', now);
  const scoped = await get('/v1/dashboard/state');
  assert.ok(scoped.tasks.every(t => t.created_by === 'agent-a' || t.assignee === 'agent-a'));
  assert.ok(!scoped.news.some(n => n.ref === privateTask), 'agent dashboard feed does not expose hidden private task activity');
  assert.ok(scoped.messages.every(m => m.sender === 'agent-a' || m.recipient === 'agent-a'));
  assert.ok(scoped.events.every(e => e.recipient === 'agent-a'));
  assert.ok(scoped.audit.every(a => a.actor === 'agent-a'));
  assert.ok(scoped.questions.every(q => q.sender === 'agent-a' || q.recipient === 'agent-a'));
  const fullTranscript = await room.fetch(new Request('https://mesh.example/internal/transcript', {
    headers: { 'x-agentmesh-agent': 'legacy-adapter' },
  }));
  assert.equal(fullTranscript.status, 200);
  assert.ok((await fullTranscript.json()).tasks.some(t => t.id === privateTask));

  await post(base + '/close', { decision: 'Use typed API' });
  await post(base + '/comments', { text: 'Too late' }, 'b', 409);
  await post(base + '/votes', { ref_post_id: idea, vote: 'up' }, 'b', 409);
  assert.ok((await get('/v1/dashboard/state')).news.some(n => n.kind === 'huddle_voted'));
  for (const [text, decision, note] of [['First version', 'approve', 'Looks\u0000 good'], ['Second version', 'reject', 'Needs detail']]) {
    await post('/v1/master/sections', { section: 'Goal', text }, 'a', 201);
    await post('/v1/master/submit', {});
    await post('/v1/master/vote', { decision, note }, 'b');
  }
  const promptSnapshot = await get('/v1/dashboard/state');
  const promptVotes = promptSnapshot.news.filter(n => n.kind === 'master_voted');
  assert.deepEqual(promptVotes.map(n => promptSnapshot.feed_details[String(n.id)]), [
    { decision: 'approve', note: 'Looks good', revision: 1 },
    { decision: 'reject', note: 'Needs detail', revision: 2 },
  ]);
  for (const item of (await get('/v1/news?peek=true')).news) assert.deepEqual(Object.keys(item).sort(), ['actor', 'created_at', 'id', 'kind', 'ref', 'summary']);
  console.log('Worker auth, validation, state transitions, snapshot scoping and legacy contracts passed');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => db.close());
