// Local-only owner export coverage; also supplies the real Worker fixture to Python tests.
const assert = require('node:assert/strict');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { load, context, root } = require('./runtime.cjs');

const quote = name => '"' + name.replaceAll('"', '""') + '"';
const plain = value => JSON.parse(JSON.stringify(value));
const sha = text => createHash('sha256').update(text).digest('hex');
const memberToken = 'export-member-token-not-an-admin-'.repeat(2);
const excluded = name => (name.startsWith('sqlite_') && name !== 'sqlite_sequence') || name.startsWith('_cf_');

function setup() {
  const worker = load(path.join(root, 'src/index.ts'));
  const legacy = context(), database = context();
  const limiterKeys = [];
  let rpcReads = 0;
  const env = {
    AGENT_A_TOKEN: 'export-agent-a-'.repeat(4),
    AGENT_B_TOKEN: 'export-agent-b-'.repeat(4),
    VIEWER_TOKEN: 'export-former-viewer-'.repeat(3),
    ADMIN_EXPORT_TOKEN: 'export-owner-only-secret-'.repeat(3),
    ADMIN_EXPORT_LIMITER: { limit: async ({ key }) => { limiterKeys.push(key); return { success: true }; } },
    // The owner route must not borrow or consume a live route's limiter.
    LIMITER: { limit: async () => { throw new Error('export used the legacy limiter'); } },
  };
  const room = new worker.Room(legacy.ctx, env), hub = new worker.HubRoom(database.ctx, env);
  const stub = (instance, name) => ({
    fetch: () => { throw new Error('export must not call ' + name + '.fetch (auth/seed/presence writes)'); },
    exportData: () => { rpcReads++; return instance.exportData(); },
  });
  env.ROOM = {
    idFromName: name => { assert.equal(name, 'default-room'); return name; },
    get: () => stub(room, 'Room'),
  };
  env.HUB = {
    idFromName: name => { assert.equal(name, 'openhuddle-hub'); return name; },
    get: () => stub(hub, 'HubRoom'),
  };
  const request = (token = env.ADMIN_EXPORT_TOKEN, method = 'GET', extra = {}) => {
    const headers = { 'cf-connecting-ip': '192.0.2.10', ...extra };
    if (token !== null) headers.authorization = 'Bearer ' + token;
    return worker.default.fetch(new Request('https://mesh.example/admin/export', { method, headers }), env);
  };
  return { worker, env, legacy, database, room, hub, limiterKeys, request,
    reads: () => rpcReads, close: () => { legacy.db.close(); database.db.close(); } };
}

function appTables(db) {
  return db.prepare("SELECT name, sql FROM sqlite_schema WHERE type='table' ORDER BY name").all().filter(row => !excluded(row.name));
}

// Populate the full current inventory, including legacy coordination tables that
// dashboard snapshots omit. Defaults/nullable fields stay true to their schema.
function populate(db, kind) {
  const tables = appTables(db).sort((a, b) => Number(b.name === 'teams') - Number(a.name === 'teams'));
  for (const table of tables) {
    if (table.name === 'sqlite_sequence' || db.prepare('SELECT COUNT(*) AS n FROM ' + quote(table.name)).get().n) continue;
    const columns = db.prepare('PRAGMA table_info(' + quote(table.name) + ')').all();
    const values = columns.map(column => {
      if (column.name === 'team_id' || (kind === 'hub' && table.name === 'teams' && column.name === 'id')) return 'fixture-team';
      if (table.name === 'members' && column.name === 'token_hash') return sha(memberToken);
      if (table.name === 'members' && column.name === 'role') return 'owner';
      if (table.name === 'members' && column.name === 'removed_at') return null;
      if (table.name === 'teams' && column.name === 'prompt') return JSON.stringify({ state: 'draft', version: 0, locked_version: null, sections: [{ title: 'Idea', body: 'Fixture master prompt', updated_by: null, updated_at: null }], locked_sections: null, votes: {} });
      if (column.name === 'state' || column.name === 'status') return 'open';
      if (column.name === 'agent' || column.name === 'author' || column.name === 'actor' || column.name === 'sender' || column.name === 'recipient' || column.name === 'assignee' || column.name === 'created_by' || column.name === 'owner') return 'agent-a';
      if (/INT|REAL|NUM|DOUBLE|FLOAT/i.test(column.type)) return 1;
      if (!column.notnull && !column.pk) return null;
      if (column.name === 'settings') return '{}';
      if (['acceptance_criteria', 'file_areas', 'depends_on', 'area', 'people'].includes(column.name)) return '[]';
      return kind + '-' + table.name + '-' + column.name;
    });
    db.prepare('INSERT INTO ' + quote(table.name) + ' (' + columns.map(column => quote(column.name)).join(',') + ') VALUES (' + columns.map(() => '?').join(',') + ')').run(...values);
  }
  // Deleted AUTOINCREMENT values must survive, not just MAX(id) of retained rows.
  db.exec('INSERT INTO events (' + (kind === 'hub' ? 'team_id,kind,data,created_at' : 'recipient,type,ref,actor,created_at') + ') VALUES (' + (kind === 'hub' ? "'fixture-team','sample','{}',2" : "'agent-b','sample','fixture','agent-a',2") + ')');
  const sequence = db.prepare("SELECT seq FROM sqlite_sequence WHERE name='events'").get().seq;
  db.prepare('DELETE FROM events WHERE id=?').run(sequence);
  // A future application table, quoted identifiers, nulls and binary bytes must
  // round-trip without being guessed from today's hard-coded schema inventory.
  db.exec('CREATE TABLE "future ""table" (id INTEGER PRIMARY KEY AUTOINCREMENT, payload BLOB NOT NULL, note TEXT, optional TEXT, note_len INTEGER GENERATED ALWAYS AS (length(note)) STORED, next_id INTEGER GENERATED ALWAYS AS (id + 1) VIRTUAL);' +
    'CREATE INDEX future_payload ON "future ""table"(note);' +
    'CREATE VIEW future_view AS SELECT id,note FROM "future ""table";' +
    'CREATE TRIGGER future_note AFTER UPDATE OF note ON "future ""table" BEGIN UPDATE "future ""table" SET optional=NEW.note WHERE id=NEW.id; END;' +
    'CREATE TABLE _cf_export_internal (secret TEXT); INSERT INTO _cf_export_internal VALUES (\'not application data\');');
  const insertFuture = db.prepare('INSERT INTO "future ""table"(payload,note,optional) VALUES (?,?,?)');
  insertFuture.run(Buffer.from([0, 1, 127, 128, 255]), 'quoted " text\n雪', null);
  insertFuture.run(Buffer.alloc(0), '', '');
  insertFuture.run(Buffer.alloc(2 * 1024 * 1024 + 31, 0xab), 'beyond RPC message size', null);
}

function populateBeyondListLimits(db, kind) {
  // Existing event/snapshot APIs cap events at 500 and several collections at
  // 100/200; the owner backup must not reuse those filtered/list-limited APIs.
  const event = kind === 'hub'
    ? db.prepare("INSERT INTO events(team_id,kind,data,created_at) VALUES ('fixture-team','fixture','{}',?)")
    : db.prepare("INSERT INTO events(recipient,type,ref,actor,created_at) VALUES ('agent-b','fixture','fixture','agent-a',?)");
  for (let i = 0; i < 601; i++) event.run(1000 + i);
  if (kind === 'room') {
    const message = db.prepare("INSERT INTO messages(id,sender,recipient,text,created_at) VALUES (?,'agent-a','agent-b','fixture',?)");
    const news = db.prepare("INSERT INTO news(actor,kind,ref,summary,created_at) VALUES ('agent-a','fixture','fixture','fixture',?)");
    for (let i = 0; i < 251; i++) { message.run('fixture-message-' + i, 1000 + i); news.run(1000 + i); }
    const huddle = db.prepare("INSERT INTO huddles(id,owner,kind,topic,status,created_at) VALUES (?,'agent-a','design','fixture','open',?)");
    for (let i = 0; i < 121; i++) huddle.run('fixture-huddle-' + i, 1000 + i);
  }
}

function expectedValue(value) {
  return value instanceof Uint8Array ? { $blob: Buffer.from(value).toString('base64') } : value;
}

function assertDatabaseExport(db, exported) {
  const tables = appTables(db);
  assert.deepEqual(Object.keys(exported.tables).sort(), tables.map(table => table.name).sort(), 'every application table, including empty tables and sequence state, is exported');
  for (const table of tables) {
    const actual = exported.tables[table.name];
    assert.equal(actual.sql, table.sql, table.name + ' CREATE TABLE schema');
    const columns = db.prepare('PRAGMA table_xinfo(' + quote(table.name) + ')').all().filter(column => column.hidden !== 1).map(column => column.name);
    assert.deepEqual(actual.columns, columns, table.name + ' columns');
    const rows = db.prepare('SELECT * FROM ' + quote(table.name)).all().map(row => Object.fromEntries(Object.entries(row).map(([key, value]) => [key, expectedValue(value)])));
    assert.deepEqual(actual.rows, plain(rows), table.name + ' exports every value without snapshot caps or auth filtering');
  }
  const schema = db.prepare("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE type IN ('index','trigger','view') AND sql IS NOT NULL ORDER BY type,name").all()
    .filter(row => !excluded(row.name) && !excluded(row.tbl_name));
  assert.deepEqual([...exported.schema].sort((a, b) => (a.type + a.name).localeCompare(b.type + b.name)),
    plain(schema).sort((a, b) => (a.type + a.name).localeCompare(b.type + b.name)), 'indexes, triggers and views keep their exact SQL');
}

async function sampleExport() {
  const state = setup();
  try {
    populate(state.legacy.db, 'room'); populate(state.database.db, 'hub');
    populateBeyondListLimits(state.legacy.db, 'room'); populateBeyondListLimits(state.database.db, 'hub');
    const response = await state.request();
    assert.equal(response.status, 200, await response.clone().text());
    return response.json();
  } finally { state.close(); }
}

module.exports = async function testExport() {
  const state = setup();
  try {
    // Exporting a cold hub must bypass first-fetch seeding and member auth.
    assert.equal(state.database.db.prepare('SELECT COUNT(*) AS n FROM teams').get().n, 0);
    const initialChanges = [state.legacy.db, state.database.db].map(db => db.prepare('SELECT total_changes() AS n').get().n);
    const coldResponse = await state.request();
    assert.equal(coldResponse.status, 200, await coldResponse.clone().text());
    const cold = await coldResponse.json();
    assert.equal(cold.databases.hub.tables.teams.rows.length, 0);
    assert.equal(cold.databases.hub.tables.members.rows.length, 0);
    assert.deepEqual([state.legacy.db, state.database.db].map(db => db.prepare('SELECT total_changes() AS n').get().n), initialChanges, 'cold export performs no SQLite writes');
    assertDatabaseExport(state.legacy.db, cold.databases.room);
    assertDatabaseExport(state.database.db, cold.databases.hub);

    populate(state.legacy.db, 'room'); populate(state.database.db, 'hub');
    populateBeyondListLimits(state.legacy.db, 'room'); populateBeyondListLimits(state.database.db, 'hub');
    const before = [state.legacy.db, state.database.db].map(db => db.prepare('SELECT total_changes() AS n').get().n);
    const sqlQueries = [];
    for (const storage of [state.legacy.ctx.storage, state.database.ctx.storage]) {
      const rawExec = storage.sql.exec.bind(storage.sql);
      storage.sql.exec = (query, ...args) => { sqlQueries.push(query); return rawExec(query, ...args); };
    }
    const response = await state.request(undefined, 'GET', { 'x-agentmesh-client-ip': 'attacker-controlled' });
    assert.equal(response.status, 200, await response.clone().text());
    assert.match(response.headers.get('content-type'), /^application\/json/);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    const exported = await response.json();
    assert.equal(exported.format, 'agentmesh-sqlite-export');
    assert.equal(exported.version, 1);
    assert.equal(new Date(exported.exported_at).toISOString(), exported.exported_at);
    assert.deepEqual(Object.keys(exported.databases).sort(), ['hub', 'room']);
    assertDatabaseExport(state.legacy.db, exported.databases.room);
    assertDatabaseExport(state.database.db, exported.databases.hub);
    assert.ok(exported.databases.room.tables.events.rows.length > 500);
    assert.ok(exported.databases.hub.tables.events.rows.length > 500);
    assert.deepEqual(exported.databases.room.tables['future "table'].rows[0].payload, { $blob: 'AAF/gP8=' });
    assert.equal(Buffer.from(exported.databases.room.tables['future "table'].rows[2].payload.$blob, 'base64').length, 2 * 1024 * 1024 + 31, 'multi-megabyte export is not truncated by RPC serialization');
    assert.ok(sqlQueries.length, 'real SQLite export RPCs were called');
    for (const query of sqlQueries) assert.match(query.trim(), /^(?:SELECT\b|PRAGMA\s+(?:table_info|table_xinfo)\b)/i, 'export executes SELECT/read-only schema introspection only');
    assert.deepEqual([state.legacy.db, state.database.db].map(db => db.prepare('SELECT total_changes() AS n').get().n), before, 'export preserves every row and presence timestamp');
    assert.ok(state.limiterKeys.some(key => key.includes('192.0.2.10')), 'dedicated limiter uses trusted connection IP');
    assert.ok(state.limiterKeys.every(key => !key.includes('attacker-controlled') && !key.includes(state.env.ADMIN_EXPORT_TOKEN)), 'untrusted identity headers and secret bytes never enter limiter keys');

    const validSecret = state.env.ADMIN_EXPORT_TOKEN;
    const reads = state.reads();
    const globalBeforeInvalidAuth = state.limiterKeys.filter(key => key === 'export-owner').length;
    for (const token of [null, '', 'wrong-secret'.repeat(4), state.env.AGENT_A_TOKEN, state.env.AGENT_B_TOKEN, state.env.VIEWER_TOKEN, memberToken, validSecret + 'x', validSecret.slice(1)]) {
      const denied = await state.request(token);
      assert.equal(denied.status, 401, 'only the separate exact owner token authorizes an export');
    }
    assert.equal(state.reads(), reads, 'invalid auth never reaches Durable Object export RPCs');
    assert.equal(state.limiterKeys.filter(key => key === 'export-owner').length, globalBeforeInvalidAuth, 'invalid tokens cannot exhaust the authenticated global export budget');
    for (const secret of [state.env.AGENT_A_TOKEN, state.env.AGENT_B_TOKEN]) {
      state.env.ADMIN_EXPORT_TOKEN = secret;
      assert.equal((await state.request(secret)).status, 401, 'an owner credential cannot alias an existing agent token');
    }
    for (const configuredSecret of [undefined, '', 'short-admin-secret']) {
      state.env.ADMIN_EXPORT_TOKEN = configuredSecret;
      assert.notEqual((await state.request(validSecret)).status, 200, 'unset/short owner secret cannot authorize');
      assert.notEqual((await state.request(configuredSecret || '')).status, 200, 'unset/short owner secret fails closed');
    }
    state.env.ADMIN_EXPORT_TOKEN = validSecret;
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD']) {
      assert.equal((await state.request(validSecret, method)).status, 405, 'owner export is GET-only');
    }
    assert.equal(state.reads(), reads, 'wrong methods/configurations never export data');

    const limiter = state.env.ADMIN_EXPORT_LIMITER;
    state.env.ADMIN_EXPORT_LIMITER = undefined;
    assert.equal((await state.request()).status, 503, 'missing dedicated limiter fails closed');
    state.env.ADMIN_EXPORT_LIMITER = { limit: async () => ({ success: false }) };
    assert.equal((await state.request()).status, 429, 'dedicated owner limiter refuses excess exports');
    assert.equal(state.reads(), reads, 'rate-limited requests never export data');
    let calls = 0;
    state.env.ADMIN_EXPORT_LIMITER = { limit: async () => ({ success: ++calls === 1 }) };
    assert.equal((await state.request()).status, 429, 'global owner limiter also bounds exports after per-IP allowance');
    assert.equal(calls, 2, 'both per-IP and global export budgets are required');
    assert.equal(state.reads(), reads);
    state.env.ADMIN_EXPORT_LIMITER = limiter;

    // Unsupported numeric values fail the whole export rather than producing
    // rounded JSON which could pass a row-count-only comparison after import.
    state.legacy.db.exec('CREATE TABLE numeric_probe(value REAL)');
    const numeric = state.legacy.db.prepare('INSERT INTO numeric_probe(value) VALUES (?)');
    numeric.run(Number.MAX_SAFE_INTEGER + 1);
    assert.equal((await state.request()).status, 500, 'unsafe JSON integer is not silently rounded');
    state.legacy.db.exec('DELETE FROM numeric_probe');
    numeric.run(Infinity);
    assert.equal((await state.request()).status, 500, 'non-finite SQLite REAL is not silently converted to null');

    // A failed sibling snapshot must not become a valid-looking partial export,
    // and its successful sibling stream must be released rather than leaked.
    const roomBinding = state.env.ROOM, hubBinding = state.env.HUB;
    let siblingCanceled = false;
    state.env.ROOM = { idFromName: name => name, get: () => ({ exportData: async () => { throw new Error('private SQL detail'); } }) };
    state.env.HUB = { idFromName: name => name, get: () => ({ exportData: async () => new Response(new ReadableStream({
      start(controller) { controller.enqueue(new TextEncoder().encode('{}')); },
      cancel() { siblingCanceled = true; },
    })) }) };
    const failed = await state.request();
    assert.equal(failed.status, 500);
    assert.ok(!((await failed.text()).includes('private SQL detail')), 'failure does not expose internal SQL errors');
    assert.equal(siblingCanceled, true, 'successful sibling body is canceled when the other snapshot fails');

    // Client cancellation propagates to both underlying DO response streams,
    // including the second snapshot before the Worker has begun consuming it.
    const canceled = [];
    const streamed = name => ({ idFromName: id => id, get: () => ({ exportData: async () => new Response(new ReadableStream({
      start(controller) { controller.enqueue(new TextEncoder().encode('{"tables":{},"schema":[]}')); },
      cancel() { canceled.push(name); },
    })) }) });
    state.env.ROOM = streamed('room'); state.env.HUB = streamed('hub');
    const cancellable = await state.request();
    assert.equal(cancellable.status, 200);
    const reader = cancellable.body.getReader();
    await reader.read();
    await reader.cancel();
    assert.deepEqual(canceled.sort(), ['hub', 'room'], 'client cancellation releases both pending snapshot streams');
    state.env.ROOM = roomBinding; state.env.HUB = hubBinding;
  } finally { state.close(); }
};

if (require.main === module) {
  (process.argv.includes('--fixture')
    ? sampleExport().then(value => process.stdout.write(JSON.stringify(value)))
    : module.exports().then(() => console.log('Worker owner-only complete read-only export passed')))
    .catch(error => { console.error(error); process.exitCode = 1; });
}
