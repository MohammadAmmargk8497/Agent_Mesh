// Owner-only recovery RPC support. No migrations, updates, or deletions.
import { HttpError } from "../coordination";
import { exportTables, validExportCursor } from "./export";

export const IMPORT_BODY_BYTES = 2 * 1024 * 1024;
const RETENTION_MS = 30 * 86_400_000;
const quote = (name: string) => '"' + name.replaceAll('"', '""') + '"';
const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

function cell(value: unknown): SqlStorageValue {
  if (value === null || typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value) &&
      (!Number.isInteger(value) || Number.isSafeInteger(value))) return value;
  if (object(value) && Object.keys(value).length === 1 && typeof value.$base64 === "string" &&
      /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value.$base64)) {
    const binary = atob(value.$base64);
    // Reject noncanonical encodings rather than silently changing backup values.
    if (btoa(binary) !== value.$base64) throw new HttpError(400, "invalid base64 cell");
    return Uint8Array.from(binary, character => character.charCodeAt(0)).buffer;
  }
  throw new HttpError(400, "unsupported import cell");
}

export class AdminRecovery {
  private pending = false;
  constructor(private ctx: DurableObjectState) {}

  assertReady() {
    if (this.pending) throw new HttpError(503, "store restart pending");
  }

  async prepareRestore(requestedTime: number, dryRun: boolean) {
    this.assertReady();
    if (!Number.isSafeInteger(requestedTime) || requestedTime < 0 || requestedTime > Date.now())
      throw new HttpError(400, "time must be a past epoch millisecond integer");
    // Cloudflare requires a time within 30 days. One second avoids the moving
    // retention edge expiring while the bookmark lookup is in flight. This is
    // the requested lookup time, NOT an undocumented bookmark commit timestamp.
    const time = Math.max(requestedTime, Date.now() - RETENTION_MS + 1000);
    const bookmark = await this.ctx.storage.getBookmarkForTime(time);
    const result = { requested_time: requestedTime, time, bookmark, dry_run: dryRun };
    if (dryRun) return result;
    this.assertReady();
    this.pending = true;
    try {
      const undo_bookmark = await this.ctx.storage.onNextSessionRestoreBookmark(bookmark);
      return { ...result, undo_bookmark, restore_scheduled: true };
    } catch (err) { this.pending = false; throw err; }
  }

  abortRestore() {
    if (!this.pending) throw new HttpError(409, "no restore is scheduled");
    // abort is uncatchable and cannot return an RPC response. The outer Worker
    // already received the durable scheduling result before making this call.
    this.ctx.abort("owner recovery restart");
  }

  importRows(table: string, rows: unknown) {
    try { return this.insertRows(table, rows); }
    catch (err) {
      if (err instanceof HttpError) return { error: err.message, status: err.status };
      return { error: "import failed; no rows in this batch were changed", status: 409 };
    }
  }

  private insertRows(table: string, rows: unknown) {
    this.assertReady();
    if (!Array.isArray(rows) || rows.length > 1000) throw new HttpError(400, "rows must be an array of at most 1000 rows");
    const sql = this.ctx.storage.sql;
    if (!exportTables(sql).some(candidate => candidate.name === table))
      throw new HttpError(404, "import table not found");
    const columns = sql.exec(`PRAGMA table_info(${quote(table)})`).toArray();
    const names = columns.map(column => String(column.name));
    const primary = columns.filter(column => Number(column.pk) > 0)
      .sort((a, b) => Number(a.pk) - Number(b.pk)).map(column => String(column.name));
    if (names.includes("rowid") || names.includes("__agentmesh_export_rowid"))
      throw new HttpError(409, "table shadows the export rowid");
    // Insert triggers could mutate/delete existing rows despite OR IGNORE.
    // Clear UPDATE/DELETE-only trigger headers cannot fire during an import.
    const triggers = sql.exec("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", table).toArray();
    if (triggers.some(trigger => {
      const header = String(trigger.sql).split(/\bBEGIN\b/i)[0];
      return /\bINSERT\b/i.test(header) || !/\b(?:BEFORE|AFTER)\s+(?:DELETE|UPDATE(?:\s+OF\b[\s\S]*?)?)\s+ON\b/i.test(header);
    })) throw new HttpError(409, "import tables with insert or unrecognized triggers are unsupported");
    const prepared = rows.map(row => {
      if (!object(row)) throw new HttpError(400, "each import row must be an object");
      const envelope = object(row.data) && Object.hasOwn(row, "rowid");
      const values = envelope ? row.data as Record<string, unknown> : row;
      if (envelope && Object.keys(row).some(key => key !== "data" && key !== "rowid"))
        throw new HttpError(400, "unknown import row envelope field");
      if (Object.keys(values).some(name => !names.includes(name))) throw new HttpError(400, "unknown import column");
      if (names.some(name => !Object.hasOwn(values, name))) throw new HttpError(400, "import rows must include every column");
      const bound = names.map(name => cell(values[name]));
      const rowid = envelope ? row.rowid : null;
      // SQLite's nullable TEXT/composite primary keys are not truly unique.
      // As in the backup verifier, use rowid if any PK component is NULL.
      const retainRowid = (!primary.length && table !== "sqlite_sequence") ||
        primary.some(name => values[name] === null);
      if (retainRowid &&
          (typeof rowid !== "string" || !validExportCursor(rowid)))
        throw new HttpError(400, "rows without a non-null primary key require an exported rowid");
      if (table === "sqlite_sequence" && typeof values.name !== "string")
        throw new HttpError(400, "sqlite_sequence requires a name");
      return { bound, rowid, retainRowid };
    });
    return this.ctx.storage.transactionSync(() => {
      let inserted = 0;
      for (const { bound, rowid, retainRowid } of prepared) {
        // sqlite_sequence has no declared unique key: its logical key is name.
        // Other keyless tables retain their exact signed 64-bit exported rowid.
        const key = retainRowid ? [] : primary.length ? primary : table === "sqlite_sequence" ? ["name"] : [];
        const where = key.length ? key.map(name => `${quote(name)} IS ?`).join(" AND ") : "rowid=CAST(? AS INTEGER)";
        const params = key.length ? key.map(name => bound[names.indexOf(name)]) : [rowid as string];
        if (sql.exec(`SELECT 1 FROM ${quote(table)} WHERE ${where} LIMIT 1`, ...params).toArray().length) continue;
        sql.exec(`INSERT OR IGNORE INTO ${quote(table)} (${retainRowid ? "rowid," : ""}${names.map(quote).join(",")}) VALUES (${retainRowid ? "CAST(? AS INTEGER)," : ""}${names.map(() => "?").join(",")})`,
          ...(retainRowid ? [rowid as string, ...bound] : bound)).toArray();
        inserted += Number(sql.exec("SELECT changes() AS n").one().n);
      }
      return { inserted, skipped: rows.length - inserted };
    });
  }
}
