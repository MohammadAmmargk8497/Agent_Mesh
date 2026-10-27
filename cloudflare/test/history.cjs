// The same history contract runs against the Worker port and Python-hosted hub.
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

function exerciseHubHistory(Hub, db, huddles, limits) {
  const old = Date.UTC(2020, 0, 1);
  let now = old;
  // A legacy retention option must not revive inactive-team deletion.
  const hub = new Hub(db, { now: () => now, retentionDays: 1 });
  const created = hub.createTeam({ team_name: 'Permanent history', member_name: 'Owner' });
  const joined = hub.join({ code: created.invite_url, member_name: 'Peer' });
  const team = created.team_id;
  const owner = hub.findMember(team, created.member_id);
  const peer = hub.findMember(team, joined.member_id);
  const count = table => hub.get(`SELECT COUNT(*) AS n FROM ${table} WHERE team_id = ?`, team).n;
  const historyTables = ['teams', 'members', 'events', 'prompt_versions', 'tasks', 'asks', 'huddles', 'huddle_posts', 'huddle_votes'];
  const snapshot = () => Object.fromEntries(historyTables.map(table => [table,
    JSON.stringify(hub.all(`SELECT * FROM ${table} ORDER BY rowid`)),
  ]));

  hub.plan(owner, { tasks: [{ key: 'OLD', title: 'Retain this task' }] });
  const closedCount = limits.huddles_closed_kept + 1;
  const postsPerHuddle = Math.ceil(limits.huddle_posts_per_team / closedCount);
  hub.tx(() => {
    for (let i = 1; i <= closedCount; i++) {
      hub.run("INSERT INTO huddles (team_id,id,kind,topic,started_by,status,decision,created_at,updated_at,closed_at,next_post) VALUES (?,?,'brainstorm','Old discussion',?,'closed','Keep it',?,?,?,?)",
        team, `H${i}`, owner.id, old, old, old, postsPerHuddle + 1);
      for (let seq = 1; seq <= postsPerHuddle; seq++) {
        hub.run("INSERT INTO huddle_posts (team_id,huddle_id,seq,member_id,kind,text,created_at) VALUES (?,?,?,?,'idea','Keep every idea',?)",
          team, `H${i}`, seq, owner.id, old);
      }
      hub.run('INSERT INTO huddle_votes (team_id,huddle_id,seq,member_id,value,created_at) VALUES (?,?,1,?,1,?)',
        team, `H${i}`, peer.id, old);
    }
    hub.run('UPDATE teams SET next_huddle = ? WHERE id = ?', closedCount + 1, team);
    for (let i = 1; i <= limits.asks_per_team + 1; i++) {
      hub.run('INSERT INTO asks (team_id,id,from_id,to_id,question,answer,created_at,answered_at) VALUES (?,?,?,?,?,?,?,?)',
        team, `Q${i}`, owner.id, peer.id, 'Old question', i % 2 ? 'Old answer' : null, old, i % 2 ? old : null);
    }
    // Ask's former count sweep ran only at every fiftieth question.
    hub.run('UPDATE teams SET next_ask = ? WHERE id = ?', Math.ceil((limits.asks_per_team + 2) / 50) * 50, team);
  });

  const postsBefore = count('huddle_posts');
  const votesBefore = count('huddle_votes');
  assert.ok(postsBefore >= limits.huddle_posts_per_team);
  const discussion = hub.huddleStart(owner, { topic: 'New discussion after a full archive' }).huddle;
  hub.huddlePost(owner, { id: discussion.id, kind: 'idea', text: 'A new idea never evicts an old discussion' });
  assert.equal(count('huddles'), closedCount + 1, 'posting beyond the former team capacity retains every huddle');
  assert.equal(count('huddle_posts'), postsBefore + 1, 'posting retains every archived post');
  assert.equal(count('huddle_votes'), votesBefore, 'posting retains every archived vote');
  hub.huddleClose(owner, { id: discussion.id, decision: 'Retain the complete discussion' });
  assert.equal(count('huddles'), closedCount + 1, 'closing beyond the former count cap retains every huddle');
  assert.equal(count('huddle_posts'), postsBefore + 1);
  assert.equal(count('huddle_votes'), votesBefore);

  const asksBefore = count('asks');
  hub.ask(owner, { to: peer.id, question: 'One more question beyond the former count cap' });
  assert.equal(count('asks'), asksBefore + 1, 'asking retains answered and unanswered questions beyond the former cap');
  for (let version = 1; version <= limits.prompt_versions_kept + 2; version++) {
    hub.promptEdit(owner, { section: 'Idea', text: `Archived prompt revision ${version}` });
  }
  assert.equal(count('prompt_versions'), limits.prompt_versions_kept + 2, 'editing retains every prompt version');
  assert.equal(hub.get('SELECT created_at FROM prompt_versions WHERE team_id = ? AND version = 1', team).created_at, old);

  // Fill through the actual event threshold, not merely an old timestamp.
  const nextBoundary = Math.ceil((limits.events_per_team + 1) / 500) * 500;
  hub.tx(() => {
    for (let id = hub.lastEventId(team) + 1; id < nextBoundary; id++) {
      hub.run("INSERT INTO events (team_id,member_id,kind,data,created_at) VALUES (?,?,'archived','{}',?)", team, owner.id, old);
    }
  });
  const eventsBefore = count('events');
  const emitted = hub.emit(team, owner.id, 'threshold', { retained: true });
  assert.equal(emitted.id, nextBoundary);
  assert.equal(count('events'), eventsBefore + 1, 'emit retains every event at the former count sweep boundary');
  assert.ok(count('events') > limits.events_per_team);

  now = Date.UTC(2030, 0, 1);
  hub.run('INSERT INTO claims (team_id,path,member_id,expires_at) VALUES (?,?,?,?)', team, 'expired.ts', owner.id, old);
  hub.run('INSERT INTO claims (team_id,path,member_id,expires_at) VALUES (?,?,?,?)', team, 'active.ts', owner.id, now + 86400000);
  const before = snapshot();
  huddles.sweep(hub);
  assert.deepEqual(snapshot(), before, 'direct former huddle sweep retains backdated history');
  for (let i = 0; i < 2; i++) {
    hub.sweep();
    assert.deepEqual(snapshot(), before, 'housekeeping retains all backdated rows, including inactive teams');
  }
  assert.equal(hub.get('SELECT path FROM claims WHERE team_id = ? AND path = ?', team, 'expired.ts'), null);
  assert.equal(hub.get('SELECT path FROM claims WHERE team_id = ? AND path = ?', team, 'active.ts').path, 'active.ts');

  // Retention must not disable explicit live-state release or destructive owner actions.
  hub.claims(peer, { paths: ['release.ts'] });
  assert.equal(hub.claims(peer, { action: 'release', paths: ['release.ts'] }).released, 1);
  hub.claims(peer, { paths: ['member-claim.ts'] });
  hub.removeMember(owner, { member: peer.id });
  assert.equal(hub.get('SELECT removed_at FROM members WHERE id = ?', peer.id).removed_at, now);
  assert.equal(hub.get('SELECT path FROM claims WHERE member_id = ?', peer.id), null);
  hub.plan(owner, { replace: true, tasks: [{ key: 'NEW', title: 'Explicit replacement' }] });
  assert.equal(hub.get('SELECT key FROM tasks WHERE team_id = ? AND key = ?', team, 'OLD'), null);
  hub.deleteOwnTeam(owner);
  assert.equal(hub.get('SELECT id FROM teams WHERE id = ?', team), null);
  for (const table of [...historyTables.filter(table => table !== 'teams'), 'claims']) {
    assert.equal(count(table), 0, `owner deletion still removes ${table}`);
  }
}

module.exports = function testWorkerHubHistory() {
  const { load, context, root } = require('./runtime.cjs');
  const { DoSqlite } = load(path.join(root, 'src/openhuddle/do-sqlite.ts'));
  const { openDb } = load(path.join(root, 'src/openhuddle/src/hub/db-do.js'));
  const { Hub } = load(path.join(root, 'src/openhuddle/src/hub/hub.js'));
  const huddles = load(path.join(root, 'src/openhuddle/src/hub/huddles.js'));
  const { LIMITS } = load(path.join(root, 'src/openhuddle/src/shared/protocol.js'));
  const storage = context();
  try {
    exerciseHubHistory(Hub, openDb(new DoSqlite(storage.ctx.storage)), huddles, LIMITS);
  } finally { storage.db.close(); }
};

if (require.main === module) {
  (async () => {
    if (process.argv[2] === '--server') {
      const directory = path.resolve(__dirname, '../../server/src/agentmesh_server/vendor/openhuddle/src');
      const get = name => import(pathToFileURL(path.join(directory, name)).href);
      const [{ Hub }, { openDb }, huddles, { LIMITS }] = await Promise.all([
        get('hub/hub.js'), get('hub/db.js'), get('hub/huddles.js'), get('shared/protocol.js'),
      ]);
      const db = openDb();
      try { exerciseHubHistory(Hub, db, huddles, LIMITS); }
      finally { db.close(); }
      console.log('Python-hosted hub permanent-history regression passed');
    } else {
      module.exports();
      console.log('Worker hub permanent-history regression passed');
    }
  })().catch(error => { console.error(error); process.exitCode = 1; });
}
