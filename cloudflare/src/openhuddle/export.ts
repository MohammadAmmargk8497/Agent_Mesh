// Read-only, bounded exports shared by the two AgentMesh SQLite stores.
export const EXPORT_PAGE_LIMIT = 1000;
const PAGE_BYTES = 2 * 1024 * 1024;
const ROWID = "__agentmesh_export_rowid";
// _cf_* belongs to the storage runtime, not AgentMesh. Keep sqlite_sequence:
// its high-water marks are needed even when the corresponding rows were deleted.
const TABLES = "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT GLOB '_cf_*' AND (name NOT GLOB 'sqlite_*' OR name='sqlite_sequence') ORDER BY name";
const quote = (name: string) => '"' + name.replaceAll('"', '""') + '"';
type ExportPage = {
  rows: Array<{ rowid: string; data: Record<string, ExportCell> }>;
  next_after: string | null;
  done: boolean;
} | { error: string; status: number };
type ExportCell = string | number | null | { $base64: string };

export function exportTables(sql: SqlStorage) {
  return sql.exec<{ name: string; sql: string }>(TABLES).toArray().map(table => ({
    ...table,
    count: Number(sql.exec(`SELECT COUNT(*) AS n FROM ${quote(table.name)}`).one().n),
  }));
}

export function validExportCursor(after: string | null): boolean {
  return after === null || (/^(?:0|-?[1-9]\d*)$/.test(after) && after.length <= 20 &&
    BigInt(after) >= -(1n << 63n) && BigInt(after) < (1n << 63n));
}

export function exportPage(sql: SqlStorage, table: string, after: string | null, limit: number): ExportPage {
  if (!validExportCursor(after) || !Number.isInteger(limit) || limit < 1 || limit > EXPORT_PAGE_LIMIT)
    return { error: "invalid export cursor or limit", status: 400 };
  // Bind the requested name against the actual schema before quoting identifiers.
  if (!sql.exec<{ name: string }>(TABLES).toArray().some(row => row.name === table))
    return { error: "export table not found", status: 404 };
  const columns = sql.exec(`PRAGMA table_info(${quote(table)})`).toArray();
  if (columns.some(column => column.name === ROWID || column.name === "rowid"))
    return { error: "table shadows the export rowid", status: 409 };
  const cursor = after === null
    ? sql.exec(`SELECT CAST(rowid AS TEXT) AS ${ROWID}, * FROM ${quote(table)} ORDER BY rowid LIMIT ?`, limit)
    : sql.exec(`SELECT CAST(rowid AS TEXT) AS ${ROWID}, * FROM ${quote(table)} WHERE rowid > CAST(? AS INTEGER) ORDER BY rowid LIMIT ?`, after, limit);
  const rows: Array<{ rowid: string; data: Record<string, ExportCell> }> = [];
  let size = 1024; // Leave space for the small HTTP envelope and cursor.
  let next_after: string | null = null;
  for (const raw of cursor) {
    const { [ROWID]: rowid, ...values } = raw;
    const data: Record<string, ExportCell> = {};
    // No current schema has blobs, but preserve them rather than serializing {}.
    for (const [key, value] of Object.entries(values as Record<string, unknown>)) {
      if (value instanceof ArrayBuffer || value instanceof Uint8Array) {
        const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
        let binary = "";
        for (let i = 0; i < bytes.length; i += 8192)
          binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
        data[key] = { $base64: btoa(binary) };
      } else if (typeof value === "number" && (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))) {
        return { error: "export contains an unsafe numeric value", status: 409 };
      } else if (value === null || typeof value === "number" || typeof value === "string") data[key] = value;
      else return { error: "unsupported export cell type", status: 409 };
    }
    const row = { rowid: String(rowid), data };
    const bytes = new TextEncoder().encode(JSON.stringify(row)).byteLength + 1;
    if (size + bytes > PAGE_BYTES) {
      if (!rows.length) return { error: "single export row exceeds page byte limit", status: 413 };
      break;
    }
    size += bytes;
    rows.push(row);
    next_after = row.rowid;
  }
  // An empty page is the unambiguous terminator. A short page can simply have
  // reached the byte budget, so clients must not stop based on row count alone.
  return { rows, next_after, done: rows.length === 0 };
}
