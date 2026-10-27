// No network: exercise owner routing, recovery lifecycle and real SQLite imports.
const assert = require('node:assert/strict');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { load, context, root } = require('./runtime.cjs');

module.exports = async function testRecovery() {
  const worker = load(path.join(root, 'src/index.ts'));
  const env = {
    AGENT_A_TOKEN: 'a'.repeat(32) + ',' + 'retired-a'.repeat(4),
    AGENT_B_TOKEN: 'b'.repeat(32), VIEWER_TOKEN: 'v'.repeat(32) + ',' + 'retired-v'.repeat(4),
    ADMIN_EXPORT_TOKEN: 'owner-only-recovery'.repeat(3),
    LIMITER: { limit: () => { throw new Error('recovery must use the owner limiter'); } },
  };
  const keys = [];
  const limiter = { limit: async ({ key }) => { keys.push(key); return { success: true }; } };
  env.ADMIN_EXPORT_LIMITER = limiter;
  const stores = { room: context(), hub: context() };
  let room = new worker.Room(stores.room.ctx, env), hub = new worker.HubRoom(stores.hub.ctx, env);
  env.ROOM = { idFromName: name => { assert.equal(name, 'default-room'); return name; }, get: () => room };
  env.HUB = { idFromName: name => { assert.equal(name, 'openhuddle-hub'); return name; }, get: () => hub };
  const timeline = [];
  for (const [store, { ctx }] of Object.entries(stores)) {
    ctx.storage.getBookmarkForTime = async time => { timeline.push([store, 'lookup', time]); return 'bookmark-' + time; };
    ctx.storage.onNextSessionRestoreBookmark = async bookmark => {
      timeline.push([store, 'schedule', bookmark]); return 'undo-' + bookmark;
    };
    ctx.abort = () => {
      timeline.push([store, 'abort']);
      if (store === 'room') room = new worker.Room(ctx, env);
      else hub = new worker.HubRoom(ctx, env);
      throw new Error('expected Durable Object reset');
    };
  }
  const url = (route, store, extra = {}) => '/admin/' + route + '?' + new URLSearchParams({ store, ...extra });
  const restore = (store, time = 0, dry = '1') => url('restore', store, { time: String(time), dry_run: dry });
  const imports = (store, table) => url('import', store, { table });
  async function request(route, body, token = env.ADMIN_EXPORT_TOKEN, method = 'POST', extra = {}) {
    return worker.default.fetch(new Request('https://mesh.example' + route, {
      method, headers: { 'cf-connecting-ip': '127.0.0.1', ...extra,
        ...(token ? { authorization: 'Bearer ' + token } : {}) },
      ...(body === undefined ? {} : { body: typeof body === 'string' ? body : JSON.stringify(body) }),
    }), env);
  }
  async function call(route, body, status = 200, token = env.ADMIN_EXPORT_TOKEN) {
    const response = await request(route, body, token);
    const data = await response.json();
    assert.equal(response.status, status, route + ': ' + JSON.stringify(data));
    assert.equal(response.headers.get('cache-control'), 'no-store');
    return data;
  }
  const count = store => stores[store].db.prepare('SELECT total_changes() AS n').get().n;
  try {
    // Cold dry runs neither initialize schemas nor schedule alarms/restores.
    const before = Date.now();
    for (const store of ['room', 'hub']) {
      const dry = await call(restore(store));
      assert.equal(dry.store, store); assert.equal(dry.dry_run, true);
      assert.equal(dry.requested_time, 0);
      assert.ok(dry.time >= before - 30 * 86400000 + 1000);
      assert.ok(dry.time <= Date.now() - 30 * 86400000 + 1000);
      assert.equal(dry.bookmark, 'bookmark-' + dry.time);
      assert.equal(count(store), 0);
      assert.equal(stores[store].db.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'").get().n, 0);
      assert.equal(await stores[store].ctx.storage.getAlarm(), null);
    }
    assert.equal(timeline.filter(event => event[1] !== 'lookup').length, 0);
    for (const store of ['room', 'hub']) {
      stores[store].db.exec(`
        CREATE TABLE records (id TEXT PRIMARY KEY, value TEXT, payload BLOB);
        INSERT INTO records VALUES ('existing','live value',NULL);
        CREATE TABLE composite (team TEXT, key TEXT, value TEXT, PRIMARY KEY(team,key));
        INSERT INTO composite VALUES ('team','existing','live composite');
        CREATE TABLE keyless (value TEXT);
        CREATE TABLE nullable (id TEXT PRIMARY KEY, value TEXT);
        INSERT INTO keyless (rowid,value) VALUES (7,'live keyless');
        CREATE TABLE serial (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);
        INSERT INTO serial VALUES (50,'live serial');
        CREATE TABLE parent (id TEXT PRIMARY KEY);
        CREATE TABLE child (id TEXT PRIMARY KEY, parent TEXT REFERENCES parent(id));
        CREATE TABLE "quoted""table" ("quoted""key" TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE _cf_hidden (id TEXT PRIMARY KEY);
      `);
    }
    const member = 'ordinary-member'.repeat(3), teamOwner = 'team-owner'.repeat(4);
    stores.hub.db.exec('CREATE TABLE members (token_hash TEXT PRIMARY KEY, removed_at INTEGER);');
    for (const token of [member, teamOwner]) stores.hub.db.prepare('INSERT INTO members VALUES (?,NULL)')
      .run(createHash('sha256').update(token).digest('hex'));
    const routes = [restore('room'), restore('hub', 0, '0'), imports('room', 'records'), imports('hub', 'records')];
    const beforeDenied = [count('room'), count('hub'), timeline.length];
    for (const token of [null, 'wrong'.repeat(8), ...env.AGENT_A_TOKEN.split(','), env.AGENT_B_TOKEN,
      ...env.VIEWER_TOKEN.split(','), member, teamOwner]) {
      for (const route of routes) await call(route, { rows: [] }, 401, token);
    }
    const ownerSecret = env.ADMIN_EXPORT_TOKEN;
    for (const token of [...env.AGENT_A_TOKEN.split(','), env.AGENT_B_TOKEN,
      ...env.VIEWER_TOKEN.split(','), member, teamOwner]) {
      env.ADMIN_EXPORT_TOKEN = token;
      for (const route of routes) await call(route, { rows: [] }, 401, token);
    }
    stores.hub.db.prepare('UPDATE members SET removed_at=1 WHERE token_hash=?')
      .run(createHash('sha256').update(member).digest('hex'));
    env.ADMIN_EXPORT_TOKEN = member;
    await call(imports('room', 'records'), { rows: [] }, 401, member);
    env.ADMIN_EXPORT_TOKEN = ownerSecret;
    assert.deepEqual([count('room'), count('hub') - 1, timeline.length], beforeDenied);
    for (const store of ['room', 'hub']) {
      const body = { rows: [
        { rowid: '1', data: { id: 'existing', value: 'backup value', payload: null } },
        // Different PK sharing old rowid must still be inserted.
        { rowid: '1', data: { id: 'missing', value: 'original ✓', payload: { $base64: 'AP8R' } } },
      ] };
      const merged = await call(imports(store, 'records'), body);
      assert.equal(merged.inserted, 1); assert.equal(merged.skipped, 1);
      const rerun = await call(imports(store, 'records'), body);
      assert.equal(rerun.inserted, 0); assert.equal(rerun.skipped, 2);
      const db = stores[store].db;
      assert.equal(db.prepare('SELECT value FROM records WHERE id=?').get('existing').value, 'live value');
      assert.deepEqual(Buffer.from(db.prepare('SELECT payload FROM records WHERE id=?').get('missing').payload), Buffer.from([0,255,17]));
      const composite = await call(imports(store, 'composite'), { rows: [
        { team: 'team', key: 'existing', value: 'old' }, { team: 'other', key: 'existing', value: 'new' },
      ] });
      assert.equal(composite.inserted, 1); assert.equal(composite.skipped, 1);
      assert.equal(db.prepare('SELECT value FROM composite WHERE team=?').get('team').value, 'live composite');
      const keyless = { rows: [
        { rowid: '7', data: { value: 'old' } }, { rowid: '9007199254740993', data: { value: 'exact int64' } },
      ] };
      assert.equal((await call(imports(store, 'keyless'), keyless)).inserted, 1);
      assert.equal((await call(imports(store, 'keyless'), keyless)).inserted, 0);
      assert.equal(db.prepare('SELECT value FROM keyless WHERE rowid=7').get().value, 'live keyless');
      assert.equal(db.prepare("SELECT CAST(rowid AS TEXT) AS id FROM keyless WHERE value='exact int64'").get().id, '9007199254740993');
      const nullable = { rows: [{ rowid: '9007199254740993', data: { id: null, value: 'nullable primary key' } }] };
      assert.equal((await call(imports(store, 'nullable'), nullable)).inserted, 1);
      assert.equal((await call(imports(store, 'nullable'), nullable)).inserted, 0);
      assert.equal(db.prepare('SELECT CAST(rowid AS TEXT) AS id FROM nullable').get().id, '9007199254740993');
      await call(imports(store, 'nullable'), { rows: [{ id: null, value: 'missing rowid' }] }, 400);
      const sequence = { rows: [
        { rowid: '999', data: { name: 'serial', seq: 10 } },
        { rowid: '1', data: { name: 'historic', seq: 70 } },
      ] };
      assert.equal((await call(imports(store, 'sqlite_sequence'), sequence)).inserted, 1);
      assert.equal((await call(imports(store, 'sqlite_sequence'), sequence)).inserted, 0);
      assert.equal(db.prepare("SELECT seq FROM sqlite_sequence WHERE name='serial'").get().seq, 50);
      assert.equal(db.prepare("SELECT COUNT(*) AS n FROM sqlite_sequence WHERE name='serial'").get().n, 1);
      assert.equal((await call(imports(store, 'quoted"table'), { rows: [{ 'quoted"key': 'key', value: 'safe identifier' }] })).inserted, 1);
      // A large backup row may exceed the normal application 256KiB request cap.
      assert.equal((await call(imports(store, 'records'), { rows: [
        { id: 'large', value: 'é'.repeat(180000), payload: null },
      ] })).inserted, 1);
      for (const rows of [null, {}, Array(1001).fill({}), [null], [[]],
        [{ id: 'bad', value: 'bad', payload: null, unknown: 1 }],
        [{ id: 'bad', value: 'bad' }], [{ id: 'bad', value: 'bad', payload: { $base64: 'AB==' } }],
        [{ id: 'bad', value: true, payload: null }], [{ id: 'bad', value: 9007199254740992, payload: null }],
        [{ rowid: '1', data: { id: 'bad', value: 'bad', payload: null }, extra: 1 }],
        [{ id: 'batch-safe', value: 'must not insert', payload: null }, { unknown: 'invalid later row' }]])
        await call(imports(store, 'records'), { rows }, 400);
      assert.equal(db.prepare("SELECT COUNT(*) AS n FROM records WHERE id='batch-safe'").get().n, 0);
      await call(imports(store, 'keyless'), { rows: [{ value: 'no rowid' }] }, 400);
      await call(imports(store, 'keyless'), { rows: [{ rowid: '9223372036854775808', data: { value: 'overflow' } }] }, 400);
      await call(imports(store, 'missing'), { rows: [] }, 404);
      await call(imports(store, '_cf_hidden'), { rows: [] }, 404);
      await call(imports(store, 'records; DROP TABLE records'), { rows: [] }, 404);
      await call(imports(store, 'child'), { rows: [
        { id: 'first', parent: null }, { id: 'second', parent: 'missing-parent' },
      ] }, 409);
      assert.equal(db.prepare('SELECT COUNT(*) AS n FROM child').get().n, 0, 'failed batch rolls back');
      db.exec("CREATE TRIGGER mutate AFTER INSERT ON parent BEGIN DELETE FROM records; END;");
      await call(imports(store, 'parent'), { rows: [{ id: 'must not trigger' }] }, 409);
      assert.equal(db.prepare('SELECT COUNT(*) AS n FROM parent').get().n, 0);
      assert.equal(db.prepare('SELECT COUNT(*) AS n FROM records').get().n, 3);
      db.exec("CREATE TRIGGER harmless AFTER UPDATE OF value ON nullable BEGIN DELETE FROM keyless; END;");
      assert.equal((await call(imports(store, 'nullable'), { rows: [{ id: 'non-null', value: 'insert does not fire update trigger' }] })).inserted, 1);
      assert.equal(db.prepare('SELECT COUNT(*) AS n FROM keyless').get().n, 2);
    }
    for (const invalid of ['', '-1', '1.5', 'Infinity', '9007199254740992', String(Date.now() + 86400000)])
      await call(restore('room', invalid), undefined, 400);
    for (const invalid of ['', 'true', '2']) await call(restore('room', 0, invalid), undefined, 400);
    await call(restore('wrong'), undefined, 400);
    await call(imports('wrong', 'records'), { rows: [] }, 400);
    await call('/admin/import?store=room', { rows: [] }, 400);
    for (const body of ['[]', '{}', '{', '{"rows":[],"extra":true}'])
      await call(imports('room', 'records'), body, 400);
    for (const route of routes) assert.equal((await request(route, undefined, ownerSecret, 'GET')).status, 405);
    const advertised = await request(imports('room', 'records'), '{}', ownerSecret, 'POST', { 'content-length': String(2 * 1024 * 1024 + 1) });
    assert.equal(advertised.status, 413);
    let canceled = false;
    const streamed = await worker.default.fetch(new Request('https://mesh.example' + imports('room', 'records'), {
      method: 'POST', headers: { authorization: 'Bearer ' + ownerSecret }, duplex: 'half',
      body: new ReadableStream({ start(controller) {
        controller.enqueue(new Uint8Array(2 * 1024 * 1024)); controller.enqueue(new Uint8Array(1));
      }, cancel() { canceled = true; } }),
    }), env);
    assert.equal(streamed.status, 413); assert.equal(canceled, true);
    for (const store of ['room', 'hub']) {
      const old = store === 'room' ? room : hub;
      const counts = [count('room'), count('hub')];
      const dry = await call(restore(store, Date.now() - 5000));
      const real = await call(restore(store, dry.time, '0'));
      assert.equal(real.bookmark, dry.bookmark); assert.equal(real.dry_run, false);
      assert.equal(real.restore_scheduled, true); assert.equal(real.undo_bookmark, 'undo-' + real.bookmark);
      const events = timeline.filter(event => event[0] === store).slice(-3);
      assert.deepEqual(events.map(event => event[1]), ['lookup', 'schedule', 'abort']);
      assert.throws(() => old.exportTables(), /restart pending/);
      assert.equal(old.importRows('records', []).status, 503);
      assert.equal((await worker.default.fetch(new Request('https://mesh.example/admin/export/tables', {
        headers: { authorization: 'Bearer ' + ownerSecret },
      }), env)).status, 200, 'new session exports are ready');
      assert.deepEqual([count('room'), count('hub')], counts);
    }
    const lookup = stores.room.ctx.storage.getBookmarkForTime;
    const schedule = stores.room.ctx.storage.onNextSessionRestoreBookmark;
    const aborts = timeline.filter(event => event[1] === 'abort').length;
    stores.room.ctx.storage.getBookmarkForTime = async () => { throw new Error('history unavailable'); };
    await call(restore('room', 0, '0'), undefined, 500);
    stores.room.ctx.storage.getBookmarkForTime = lookup;
    stores.room.ctx.storage.onNextSessionRestoreBookmark = async () => { throw new Error('schedule failed'); };
    await call(restore('room', 0, '0'), undefined, 500);
    assert.equal(timeline.filter(event => event[1] === 'abort').length, aborts);
    assert.ok(room.exportTables().length, 'failed scheduling does not falsely mark a restart');
    stores.room.ctx.storage.onNextSessionRestoreBookmark = schedule;
    // A reset that fails to occur must never let polling mistake the old session
    // for restored readiness and import rows that would then disappear.
    for (const store of ['room', 'hub']) {
      stores[store].ctx.abort = () => { throw new Error('reset transport unavailable'); };
      await call(restore(store, 0, '0'));
      assert.equal((await worker.default.fetch(new Request('https://mesh.example/admin/export/tables', {
        headers: { authorization: 'Bearer ' + ownerSecret },
      }), env)).status, 503);
      await call(imports(store, 'records'), { rows: [] }, 503);
      const normalLimiter = env.LIMITER;
      env.LIMITER = { limit: async () => ({ success: true }) };
      try {
        const before = count(store);
        const route = store === 'room' ? '/v1/whoami' : '/api/state';
        const response = await request(route, undefined, env.AGENT_A_TOKEN.split(',')[0], 'GET');
        assert.equal(response.status, 503, 'normal app traffic gets controlled unavailability while ' + store + ' restart is pending');
        assert.match(response.headers.get('content-type'), /^application\/json/);
        assert.equal((await response.json()).error, 'store restart pending');
        assert.equal(count(store), before, 'pending normal traffic must not initialize or modify data');
      } finally { env.LIMITER = normalLimiter; }
    }
    for (const token of [undefined, '', 'short']) {
      env.ADMIN_EXPORT_TOKEN = token;
      for (const route of routes) await call(route, undefined, 503, ownerSecret);
    }
    env.ADMIN_EXPORT_TOKEN = ownerSecret;
    env.ADMIN_EXPORT_LIMITER = undefined;
    for (const route of routes) await call(route, undefined, 503);
    env.ADMIN_EXPORT_LIMITER = { limit: async () => ({ success: false }) };
    for (const route of routes) await call(route, undefined, 429);
    env.ADMIN_EXPORT_LIMITER = { limit: async () => { throw new Error('unavailable'); } };
    for (const route of routes) await call(route, undefined, 503);
    assert.ok(keys.includes('owner')); assert.ok(keys.includes('ip:127.0.0.1'));
    console.log('Worker owner-only restore lifecycle and append-only SQLite imports passed');
  } finally { stores.room.db.close(); stores.hub.db.close(); }
};
