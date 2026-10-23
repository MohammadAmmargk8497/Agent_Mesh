#!/usr/bin/env node
// Provenance: OpenHuddle 59105e5 bin/openhuddle.js, MIT; adapted: AgentMesh config and origin-scoped Access bootstrap
import { main } from '../src/client/agentmesh-boundary.js';

main('cli', process.argv.slice(2)).then(
  (code) => {
    if (typeof code === 'number') process.exitCode = code;
  },
  (err) => {
    console.error(`openhuddle: ${err?.message || err}`);
    process.exitCode = 1;
  },
);
