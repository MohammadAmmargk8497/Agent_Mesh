// AgentMesh migration boundary: read raw SQLite, never the paginated domain API.
type ExportValue = SqlStorageValue | { $blob: string };
interface ExportTable {
  sql: string | null;
  columns: string[];
  rows: Record<string, ExportValue>[];
}
type SchemaEntry = {
  type: string;
  name: string;
  tbl_name: string;
  sql: string;
};

const quote = (name: string) => '"' + name.replaceAll('"', '""') + '"';

function encode(value: SqlStorageValue): ExportValue {
  if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
    const bytes = value instanceof ArrayBuffer
      ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    // Avoid spreading a potentially large patch/blob onto the call stack.
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return { $blob: btoa(binary) };
  }
  // Never silently round an integer that cannot be represented in JSON/JS.
  if (typeof value === "number" && (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))) {
    throw new Error("SQLite value cannot be exported losslessly as a JSON number");
  }
  return value;
}

export function exportSqlite(sql: SqlStorage) {
  const entries = sql.exec<SchemaEntry>("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").toArray()
    // _cf_ belongs to the platform, not either application. sqlite_sequence is
    // application AUTOINCREMENT state; other sqlite_ objects are engine metadata.
    .filter(entry => !entry.name.startsWith("_cf_") && !entry.tbl_name.startsWith("_cf_") &&
      (!entry.name.startsWith("sqlite_") || entry.name === "sqlite_sequence"));
  const tables = Object.fromEntries(entries.filter(entry => entry.type === "table").map(entry => {
    const columns = sql.exec(`PRAGMA table_xinfo(${quote(entry.name)})`).toArray()
      .filter(column => column.hidden !== 1).map(column => String(column.name));
    const rows = sql.exec(`SELECT * FROM ${quote(entry.name)}`).toArray()
      .map(row => Object.fromEntries(Object.entries(row).map(([name, value]) => [name, encode(value)])));
    return [entry.name, { sql: entry.sql, columns, rows } satisfies ExportTable];
  }));
  // This entire method is synchronous, so other requests cannot interleave
  // writes between these SELECTs. The two DO snapshots are not cross-DO atomic.
  return { tables, schema: entries.filter(entry => entry.type !== "table") };
}
