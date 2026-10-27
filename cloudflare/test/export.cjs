// Exercise the public owner gate and real SQLite paging, without any live API.
const assert = require('node:assert/strict');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { load, context, root } = require('./runtime.cjs');

module.exports = async function testExport() {
  const worker = load(path.join(root, 'src/index.ts'));
  const legacy = context(), database = context();
  const env = {
    AGENT_A_TOKEN: 'a'.repeat(32) + ',' + 'old-a'.repeat(8),
    AGENT_B_TOKEN: 'b'.repeat(32), VIEWER_TOKEN: 'v'.repeat(32) + ',' + 'old-v'.repeat(8),
    ADMIN_EXPORT_TOKEN: 'owner-only'.repeat(6),
    LIMITER: { limit: () => { throw new Error('exports must not use the agent limiter'); } },
  };
  const room = new worker.Room(legacy.ctx, env);
  const hub = new worker.HubRoom(database.ctx, env);
  const keys = [];
  const limiter = { limit: async ({ key }) => { keys.push(key); return { success: true }; } };
  env.ADMIN_EXPORT_LIMITER = limiter;
  let normalFetches = 0;
  env.ROOM = { idFromName: name => { assert.equal(name, 'default-room'); return name; }, get: () => ({
    exportTables: () => room.exportTables(), exportPage: (...args) => room.exportPage(...args),
    fetch: () => { normalFetches++; throw new Error('export must use read-only RPC'); },
  }) };
  env.HUB = { idFromName: name => { assert.equal(name, 'openhuddle-hub'); return name; }, get: () => ({
    exportTables: () => hub.exportTables(), exportPage: (...args) => hub.exportPage(...args),
    exportTokenIsMember: hash => hub.exportTokenIsMember(hash),
    fetch: () => { normalFetches++; throw new Error('export must not seed/authenticate the hub'); },
  }) };
  const request = (route, token = env.ADMIN_EXPORT_TOKEN, method = 'GET', extra = {}) =>
    worker.default.fetch(new Request('https://mesh.example' + route, { method, headers: {
      'cf-connecting-ip': '127.0.0.1', ...extra, ...(token ? { authorization: 'Bearer ' + token } : {}),
    } }), env);
  async function call(route, status = 200, token = env.ADMIN_EXPORT_TOKEN, method = 'GET') {
    const response = await request(route, token, method);
    const body = await response.json();
    assert.equal(response.status, status, route + ': ' + JSON.stringify(body));
    assert.equal(response.headers.get('cache-control'), 'no-store');
    return body;
  }
  const pageUrl = (store, table, after, limit = 1000) => '/admin/export/page?' + new URLSearchParams({
    store, table, limit: String(limit), ...(after === null ? {} : { after }),
  });
  try {
    // Include constructors: even a first-ever export must not create tables,
    // run migrations, seed members, or schedule retention alarms.
    assert.deepEqual(await call('/admin/export/tables'), { tables: [] });
    await call(pageUrl('room', 'messages', null), 404);
    await call(pageUrl('hub', 'members', null), 404);
    for (const store of [legacy, database]) {
      assert.equal(store.db.prepare('SELECT total_changes() AS n').get().n, 0);
      assert.equal(store.db.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'").get().n, 0);
      assert.equal(await store.ctx.storage.getAlarm(), null);
    }
    const initializedRoom = await room.fetch(new Request('https://mesh.example/v1/whoami', {
      headers: { 'x-agentmesh-agent': 'agent-a' },
    }));
    assert.equal(initializedRoom.status, 200);
    await hub.fetch(new Request('https://mesh.example/api/state', { headers: {
      authorization: 'Bearer ' + 'a'.repeat(32),
    } }));
    const teamResponse = await hub.fetch(new Request('https://mesh.example/api/teams', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ team_name: 'Export isolation', member_name: 'owner' }),
    }));
    assert.equal(teamResponse.status, 200);
    const teamOwner = await teamResponse.json();
    const memberResponse = await hub.fetch(new Request('https://mesh.example/api/join', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ code: teamOwner.invite_url, member_name: 'ordinary-member' }),
    }));
    const member = await memberResponse.json();
    const coldRoom = new worker.Room(legacy.ctx, env), coldHub = new worker.HubRoom(database.ctx, env);
    const coldChanges = [legacy, database].map(({ db }) => db.prepare('SELECT total_changes() AS n').get().n);
    assert.ok(coldRoom.exportTables().length > 0);
    assert.ok(coldHub.exportTables().length > 0);
    assert.equal(coldHub.exportTokenIsMember(createHash('sha256').update(member.token).digest('hex')), true);
    assert.equal(coldHub.seeded, false, 'exporting existing hub data must not reconcile seeded credentials');
    for (const [index, store] of [legacy, database].entries())
      assert.equal(store.db.prepare('SELECT total_changes() AS n').get().n, coldChanges[index]);
    for (const store of [legacy, database]) {
      store.db.exec('CREATE TABLE export_probe (key TEXT, value TEXT); CREATE TABLE "quote\"\"table" (payload TEXT);');
      const insert = store.db.prepare('INSERT INTO export_probe (rowid,key,value) VALUES (?,?,?)');
      insert.run(-7, 'negative', 'first'); insert.run(0, 'zero', 'second');
      for (let i = 0; i < 2507; i++) insert.run(i * 3 + 9, 'key-' + i, 'Unicode ✓ ' + i);
      // Cursor strings must retain SQLite rowids beyond JavaScript's safe range.
      store.db.exec("INSERT INTO export_probe (rowid,key,value) VALUES (9007199254740993,'large-rowid','last')");
      store.db.prepare('INSERT INTO "quote\"\"table" VALUES (?)').run('quoted identifier');
    }
    legacy.db.exec('CREATE TABLE export_bytes (payload TEXT); CREATE TABLE export_blob (payload BLOB);');
    const bytes = legacy.db.prepare('INSERT INTO export_bytes VALUES (?)');
    for (let i = 0; i < 15; i++) bytes.run('é'.repeat(100000));
    legacy.db.prepare('INSERT INTO export_blob VALUES (?)').run(Buffer.from([0, 255, 17]));
    database.db.exec('CREATE TABLE _cf_hidden (private TEXT);');
    const before = [legacy, database].map(({ db }) => db.prepare('SELECT total_changes() AS n').get().n);
    const inventory = (await call('/admin/export/tables')).tables;
    assert.ok(inventory.some(t => t.store === 'room' && t.name === 'messages'));
    assert.ok(inventory.some(t => t.store === 'hub' && t.name === 'members'));
    assert.ok(inventory.some(t => t.name === 'agentmesh_schema'));
    assert.ok(inventory.some(t => t.name === 'sqlite_sequence'));
    assert.ok(!inventory.some(t => t.name === '_cf_hidden'));
    // Every listed table, including gaps, composites, empty tables and int64 ids.
    for (const table of inventory) {
      assert.equal(typeof table.sql, 'string');
      const all = [];
      let after = null;
      while (true) {
        const page = await call(pageUrl(table.store, table.name, after));
        assert.equal(page.store, table.store); assert.equal(page.table, table.name);
        assert.ok(page.rows.length <= 1000);
        if (page.done) { assert.deepEqual(page.rows, []); assert.equal(page.next_after, null); break; }
        assert.ok(page.rows.length > 0);
        for (const row of page.rows) {
          if (after !== null) assert.ok(BigInt(row.rowid) > BigInt(after));
          after = row.rowid;
        }
        assert.equal(page.next_after, after);
        all.push(...page.rows);
      }
      assert.equal(all.length, table.count, table.store + '/' + table.name);
      const db = table.store === 'room' ? legacy.db : database.db;
      const expected = db.prepare('SELECT CAST(rowid AS TEXT) AS rowid FROM "' + table.name.replaceAll('"', '""') + '" ORDER BY rowid+0').all().map(r => r.rowid);
      assert.deepEqual(all.map(row => row.rowid), expected);
      assert.equal(new Set(all.map(row => row.rowid)).size, all.length);
      if (table.name === 'export_probe') assert.equal(all.at(-1).rowid, '9007199254740993');
      if (table.name === 'export_blob') assert.deepEqual(all[0].data.payload, { $base64: 'AP8R' });
    }
    const small = await call(pageUrl('room', 'export_probe', null, 7));
    assert.equal(small.rows.length, 7);
    assert.equal((await call(pageUrl('hub', 'export_probe', null, 99999))).rows.length, 1000);
    const bounded = await request(pageUrl('room', 'export_bytes', null));
    const text = await bounded.text();
    assert.ok(Buffer.byteLength(text) <= 2 * 1024 * 1024);
    assert.ok(JSON.parse(text).rows.length < 15, 'byte budget makes partial nonterminal pages');
    assert.equal(JSON.parse(text).done, false);
    for (const cursor of ['', '1.5', '1e3', '-0', '01', 'NaN', '9223372036854775808', '-9223372036854775809'])
      await call(pageUrl('room', 'messages', cursor), 400);
    for (const limit of [0, -1, 'x', 1.5, 'Infinity']) await call(pageUrl('room', 'messages', null, limit), 400);
    await call('/admin/export/page?store=invalid&table=messages', 400);
    await call('/admin/export/page?store=room', 400);
    await call(pageUrl('room', 'messages; DROP TABLE tasks', null), 404);
    await call(pageUrl('hub', '_cf_hidden', null), 404);
    await call('/admin/export', 404);
    for (const method of ['POST', 'PUT', 'DELETE', 'HEAD']) {
      const response = await request('/admin/export/tables', env.ADMIN_EXPORT_TOKEN, method);
      assert.equal(response.status, 405);
    }
    const denied = [null, 'bad'.repeat(12), ...env.AGENT_A_TOKEN.split(','), env.AGENT_B_TOKEN,
      ...env.VIEWER_TOKEN.split(','), teamOwner.token, member.token];
    for (const token of denied) {
      for (const route of ['/admin/export/tables', pageUrl('room', 'messages', null)]) {
        const response = await request(route, token, 'GET', { 'x-agentmesh-agent': 'owner', 'x-admin-export': 'true' });
        assert.equal(response.status, 401);
      }
    }
    const ownerSecret = env.ADMIN_EXPORT_TOKEN;
    // Misconfigured owner secret cannot elevate any known class of credential.
    for (const token of [...env.AGENT_A_TOKEN.split(','), env.AGENT_B_TOKEN, ...env.VIEWER_TOKEN.split(','), teamOwner.token, member.token]) {
      env.ADMIN_EXPORT_TOKEN = token;
      await call('/admin/export/tables', 401, token);
    }
    // Removed members' known token hashes are denied as well.
    database.db.prepare('UPDATE members SET removed_at=1 WHERE token_hash=?').run(createHash('sha256').update(member.token).digest('hex'));
    await call('/admin/export/tables', 401, member.token);
    database.db.prepare('UPDATE members SET removed_at=NULL WHERE token_hash=?').run(createHash('sha256').update(member.token).digest('hex'));
    env.ADMIN_EXPORT_TOKEN = ownerSecret;
    for (const missing of [undefined, '', 'short']) {
      env.ADMIN_EXPORT_TOKEN = missing;
      await call('/admin/export/tables', 503, ownerSecret);
    }
    env.ADMIN_EXPORT_TOKEN = ownerSecret;
    env.ADMIN_EXPORT_LIMITER = undefined;
    await call('/admin/export/tables', 503);
    env.ADMIN_EXPORT_LIMITER = { limit: async () => ({ success: false }) };
    await call('/admin/export/tables', 429);
    env.ADMIN_EXPORT_LIMITER = { limit: async () => { throw new Error('unavailable'); } };
    await call('/admin/export/tables', 503);
    env.ADMIN_EXPORT_LIMITER = limiter;
    assert.ok(keys.includes('owner')); assert.ok(keys.includes('ip:127.0.0.1'));
    assert.equal(normalFetches, 0);
    // Only the explicit remove/restore test writes above occurred during exports.
    assert.equal(legacy.db.prepare('SELECT total_changes() AS n').get().n, before[0]);
    assert.equal(database.db.prepare('SELECT total_changes() AS n').get().n, before[1] + 2);
    console.log('Worker owner-only bounded export and complete keyset paging passed');
  } finally { legacy.db.close(); database.db.close(); }
};
