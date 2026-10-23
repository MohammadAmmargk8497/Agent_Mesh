// AgentMesh launcher entry; the boundary lives in src so upstream installApp copies it.
export { main, installAccessFetch } from './src/client/agentmesh-boundary.js';
import { main } from './src/client/agentmesh-boundary.js';
try { process.exitCode = await main(process.argv[2], process.argv.slice(3)); }
catch { process.stderr.write('AgentMesh client could not start; check /api availability and your local configuration.\n'); process.exitCode = 1; }
