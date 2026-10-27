// Test-only runtime: execute actual Worker modules and SQL against Node's SQLite.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { DatabaseSync } = require('node:sqlite');
const { webcrypto, timingSafeEqual } = require('node:crypto');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'node_modules/typescript'));
webcrypto.subtle.timingSafeEqual = (a, b) => timingSafeEqual(Buffer.from(a), Buffer.from(b));
const cache = new Map();
function load(filename) {
  filename = path.resolve(filename);
  if (cache.has(filename)) return cache.get(filename).exports;
  if (filename.endsWith('.json')) return { default: JSON.parse(fs.readFileSync(filename, 'utf8')) };
  if (filename.endsWith('.html')) return { default: fs.readFileSync(filename, 'utf8') };
  const module = { exports: {} };
  cache.set(filename, module);
  const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const localRequire = id => {
    if (id === 'cloudflare:workers') return { DurableObject: class {
      constructor(ctx, env) { this.ctx = ctx; this.env = env; }
    } };
    if (id.startsWith('node:')) return require(id);
    assert.ok(id.startsWith('.'), 'unexpected import: ' + id);
    let next = path.resolve(path.dirname(filename), id);
    if (!path.extname(next)) next += '.ts';
    assert.ok(next.startsWith(path.join(root, 'src') + path.sep), 'import outside Worker source');
    return load(next);
  };
  vm.runInNewContext(source, { module, exports: module.exports, require: localRequire,
    crypto: webcrypto, Buffer, Request, Response, Headers, URL, TextEncoder, TextDecoder,
    Uint8Array, ArrayBuffer, ReadableStream, AbortController, btoa, console,
    structuredClone, setTimeout, clearTimeout, setInterval, clearInterval,
  }, { filename });
  return module.exports;
}
function context(db = new DatabaseSync(':memory:')) {
  db.exec('PRAGMA foreign_keys=ON');
  const sql = { exec(query, ...args) {
    assert.ok(!/PRAGMA\s+user_version|^\s*(?:BEGIN|COMMIT|ROLLBACK|SAVEPOINT)\b/i.test(query), 'unsupported DO SQL: ' + query);
    const before = Number(db.prepare('SELECT total_changes() AS n').get().n);
    let rows = [];
    if (!args.length && query.trim().split(';').filter(s => s.trim()).length > 1) db.exec(query);
    else {
      const statement = db.prepare(query);
      if (statement.columns().length) rows = statement.all(...args);
      else statement.run(...args);
    }
    return { [Symbol.iterator]: () => rows[Symbol.iterator](), toArray: () => rows, one: () => {
      assert.equal(rows.length, 1); return rows[0];
    // Deliberately inflate billing writes to simulate DO index/trigger accounting;
    // adapters must query SQLite changes(), never treat rowsWritten as that count.
    }, rowsWritten: (Number(db.prepare('SELECT total_changes() AS n').get().n) - before) * 3 };
  } };
  let alarm = null;
  const storage = { sql, getAlarm: async () => alarm, setAlarm: async value => { alarm = value; }, transactionSync(callback) {
    db.exec('BEGIN');
    try { const value = callback(); db.exec('COMMIT'); return value; }
    catch (error) { db.exec('ROLLBACK'); throw error; }
  } };
  return { db, ctx: { storage, blockConcurrencyWhile: callback => callback(), getWebSockets: () => [] } };
}
module.exports = { load, context, root };
