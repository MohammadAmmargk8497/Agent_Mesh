// AgentMesh-only boundary: existing config.env, Access headers and upstream machine stamps.
import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { findProject, saveConfig } from './config.js';
import { Api } from './api.js';

function isSymlink(path) {
  try { return lstatSync(path).isSymbolicLink(); } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

export function installAccessFetch(hub, env = process.env) {
  const original = globalThis.fetch;
  let origin;
  try { origin = new URL(hub).origin; } catch { return; }
  const id = env.AGENTMESH_CF_ACCESS_CLIENT_ID;
  const secret = env.AGENTMESH_CF_ACCESS_CLIENT_SECRET;
  if (!id || !secret) return;
  globalThis.fetch = (input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : input);
    if (url.origin !== origin) return original(input, init);
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    headers.set('CF-Access-Client-Id', id);
    headers.set('CF-Access-Client-Secret', secret);
    // Do not forward Access credentials across an HTTP redirect to another origin.
    return original(input, { ...init, headers, redirect: 'error' });
  };
}

function privateDirectory(directory) {
  for (let path = resolve(directory);; path = dirname(path)) {
    if (isSymlink(path)) throw new Error('AgentMesh settings must not use symbolic links');
    if (dirname(path) === path) break;
  }
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  chmodSync(directory, 0o700);
  const configDir = join(directory, '.openhuddle');
  if (isSymlink(configDir)) throw new Error('AgentMesh settings must not use symbolic links');
  mkdirSync(configDir, { recursive: true, mode: 0o700 });
  chmodSync(configDir, 0o700);
  const config = join(configDir, 'config.json');
  if (isSymlink(config)) throw new Error('AgentMesh settings must not use symbolic links');
}

const SETTINGS_KEYS = new Set([
  'AGENTMESH_URL', 'AGENTMESH_TOKEN', 'AGENTMESH_AGENT_NAME',
  'AGENTMESH_CF_ACCESS_CLIENT_ID', 'AGENTMESH_CF_ACCESS_CLIENT_SECRET',
]);

function loadSettings() {
  const config = resolve(process.env.AGENTMESH_CONFIG || join(homedir(), '.config', 'agentmesh', 'config.env'));
  for (let path = config;; path = dirname(path)) {
    if (isSymlink(path)) throw new Error('AgentMesh settings must not use symbolic links');
    if (dirname(path) === path) break;
  }
  if (existsSync(config)) {
    if (!lstatSync(config).isFile()) throw new Error('AgentMesh settings must be a regular file');
    for (let line of readFileSync(config, 'utf8').split(/\r?\n/)) {
      line = line.trim();
      if (!line || line.startsWith('#') || !line.includes('=')) continue;
      const i = line.indexOf('=');
      const key = line.slice(0, i).trim();
      if (SETTINGS_KEYS.has(key) && process.env[key] === undefined) {
        process.env[key] = line.slice(i + 1).trim().replace(/^["']+|["']+$/g, '');
      }
    }
  }
  process.env.AGENTMESH_CONFIG = config;
  process.env.OPENHUDDLE_HOME ||= join(dirname(config), 'openhuddle');
  process.env.OPENHUDDLE_HUB ||= process.env.AGENTMESH_URL || '';
}

export async function main(mode, args = []) {
  if (Number(process.versions.node.split('.')[0]) < 20) throw new Error('AgentMesh requires Node.js >= 20');
  loadSettings();
  const option = (name) => {
    const i = args.indexOf(`--${name}`);
    return i >= 0 ? args[i + 1] : args.find((arg) => arg.startsWith(`--${name}=`))?.slice(name.length + 3);
  };
  const isMcp = mode === 'mcp' || args[0] === 'mcp';
  const startDir = option('dir') || (isMcp && process.env.CLAUDE_PROJECT_DIR) || process.cwd();
  const localProject = findProject(startDir);
  const legacyHub = process.env.AGENTMESH_URL?.replace(/\/+$/, '');
  const token = process.env.AGENTMESH_TOKEN;
  const fallback = resolve(process.env.OPENHUDDLE_DIR || join(dirname(process.env.AGENTMESH_CONFIG || '.'), 'openhuddle'));
  let hub = localProject?.config.hub || legacyHub || process.env.OPENHUDDLE_HUB;
  // A join link chooses its own hub; Access credentials must follow that origin only.
  if (option('hub')) hub = option('hub');
  if (args[0] === 'join' && args[1]) {
    try { hub = new URL(args[1]).origin; } catch {}
  }
  installAccessFetch(hub);
  let project = localProject;
  if (!project && legacyHub && token && !['create', 'join', 'help', 'hub'].includes(args[0]) && !args.includes('--help') && !args.includes('--version') && (mode === 'mcp' || args.length)) {
    let state;
    try { state = await new Api(legacyHub, token).get('/api/state'); }
    catch { throw new Error('AgentMesh could not load /api/state; check the server and your AgentMesh settings.'); }
    if (!state.me?.id || !state.team?.id) throw new Error('AgentMesh hub returned an invalid identity');
    privateDirectory(fallback);
    project = saveConfig(fallback, {
      hub: legacyHub, token,
      team_id: state.team.id, team_name: state.team.name,
      member_id: state.me.id, member_name: state.me.name,
    });
  }
  if (mode === 'mcp') {
    const { runMcp } = await import('./mcp.js');
    await runMcp({ project, dir: startDir });
    return 0;
  }
  const { main: cli } = await import('./cli.js');
  // Existing env-only agents have a personal fallback; project-local teams still win.
  if (!localProject && project && !option('dir')) args = [...args, '--dir', project.root];
  return await cli(args);
}
