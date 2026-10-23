// AgentMesh boundary adapter: the node:sqlite statement surface over DO SQLite.
export class DoSqlite {
  constructor(private storage: DurableObjectStorage) {}

  prepare(query: string) {
    const rows = (...params: any[]) => this.storage.sql.exec(query, ...params);
    return {
      get: (...params: any[]) => rows(...params).toArray()[0],
      all: (...params: any[]) => rows(...params).toArray(),
      run: (...params: any[]) => {
        const cursor = rows(...params);
        // Materialize RETURNING statements before consulting connection-local metadata.
        cursor.toArray();
        // rowsWritten is Cloudflare billing (including index/trigger writes), not
        // SQLite's count of directly affected rows returned by node:sqlite.
        const metadata = this.storage.sql.exec('SELECT changes() AS changes, last_insert_rowid() AS id').one();
        return { changes: metadata.changes, lastInsertRowid: metadata.id };
      },
    };
  }

  exec(query: string) { this.storage.sql.exec(query).toArray(); }
  transactionSync<T>(callback: () => T): T { return this.storage.transactionSync(callback); }
}
