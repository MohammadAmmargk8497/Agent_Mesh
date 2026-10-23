// AgentMesh process boundary. stdin contains hashes, never the seed credentials.
import { createInterface } from 'node:readline';
import { openDb } from './src/hub/db.js';
import { Hub } from './src/hub/hub.js';
import { startHub } from './src/hub/server.js';
import { seedHub } from './agentmesh-seed.js';

const lines = createInterface({ input: process.stdin, terminal: false });
const options = await new Promise((resolve, reject) => {
  lines.once('line', (line) => {
    try { resolve(JSON.parse(line)); } catch { reject(new Error('Invalid hub startup options')); }
  });
  lines.once('close', () => reject(new Error('Missing hub startup options')));
});
lines.close();
process.umask(0o077);
const db = openDb(options.dbPath);
try {
  seedHub(new Hub(db, { publicUrl: options.publicUrl }), options.tokenHashes);
} finally {
  db.close();
}
const running = await startHub({ host: '127.0.0.1', port: 0, dbPath: options.dbPath, publicUrl: options.publicUrl, trustProxy: true });
console.log(JSON.stringify({ url: running.url }));
let closing = false;
const close = async () => {
  if (closing) return;
  closing = true;
  await running.close();
};
process.once('SIGTERM', close);
process.once('SIGINT', close);
