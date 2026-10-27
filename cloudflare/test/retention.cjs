const assert = require('node:assert/strict');
const path = require('node:path');
const { load, context, root } = require('./runtime.cjs');
const { Room } = load(path.join(root, 'src/index.ts'));
const DAY = 86400000;

module.exports = async function testRoomHistory() {
  const { db, ctx } = context();
  const env = { AGENT_A_TOKEN: 'a'.repeat(32), AGENT_B_TOKEN: 'b'.repeat(32) };
  let room = new Room(ctx, env);
  const now = Date.now(), old = now - 400 * DAY;
  const insert = (table, row) => db.prepare(
    `INSERT INTO ${table} (${Object.keys(row).join(',')}) VALUES (${Object.keys(row).map(() => '?').join(',')})`,
  ).run(...Object.values(row));
  const request = async (route, body, status = 200) => {
    const response = await room.fetch(new Request('https://mesh.example' + route, {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-agentmesh-agent': 'agent-a' },
      body: JSON.stringify(body),
    }));
    const value = await response.json();
    assert.equal(response.status, status, route + ': ' + JSON.stringify(value));
    return value;
  };
  try {
    for (const status of ['accepted', 'cancelled', 'open', 'in_progress', 'done', 'blocked']) {
      insert('tasks', { id: status, title: status, description: 'Keep task history', assignee: 'agent-b',
        created_by: 'agent-a', status, acceptance_criteria: '[]', result: 'Historical result',
        created_at: old, updated_at: old });
    }
    for (const acked_at of [old, null]) {
      insert('messages', { id: String(acked_at), sender: 'agent-a', recipient: 'agent-b',
        text: 'Keep conversation', task_id: 'accepted', created_at: old, acked_at });
    }
    insert('patches', { id: 'patch', sender: 'agent-a', title: 'Old patch', text: 'diff contents',
      task_id: 'accepted', created_at: old });
    insert('events', { recipient: 'agent-b', type: 'message', ref: 'patch', actor: 'agent-a', created_at: old });
    insert('audit', { actor: 'agent-a', action: 'message.send', ref: 'patch', created_at: old });
    for (const status of ['open', 'closed']) {
      insert('huddles', { id: status, owner: 'agent-a', kind: 'review', topic: 'Historical huddle',
        status, decision: 'Keep it', created_at: old, closed_at: status === 'closed' ? old : null });
      insert('huddle_posts', { id: status, huddle_id: status, author: 'agent-b', kind: 'idea',
        text: 'Historical proposal', created_at: old });
      insert('huddle_votes', { huddle_id: status, ref_post_id: status, agent: 'agent-a', vote: 'up',
        note: 'Historical vote', created_at: old, updated_at: old });
    }
    insert('huddle_votes', { huddle_id: 'missing', ref_post_id: 'missing', agent: 'agent-a', vote: 'up',
      note: 'Keep detached history too', created_at: old, updated_at: old });
    for (const answered_at of [old, null]) {
      insert('questions', { id: String(answered_at), sender: 'agent-a', recipient: 'agent-b',
        text: 'Keep question', answer: answered_at === null ? null : 'Keep answer',
        created_at: old, answered_at });
    }
    insert('news', { id: 1, actor: 'agent-a', kind: 'master_voted', ref: 'master',
      summary: 'Historical activity', created_at: old });
    insert('news_details', { news_id: 1, data: '{"decision":"approve"}' });
    insert('news_details', { news_id: 2, data: '{"note":"detached history"}' });
    insert('dashboard_tasks', { task_id: 'open', state: 'open' });
    insert('dashboard_tasks', { task_id: 'missing', state: 'open' });
    insert('master_sections', { section: 'Goal', text: 'Latest section', author: 'agent-a',
      revision: 2, sha256: 'hash', created_at: old });
    for (const revision of [1, 2]) insert('master_edits', { id: String(revision), section: 'Goal',
      text: 'Version ' + revision, author: 'agent-a', revision, sha256: 'hash', created_at: old });
    insert('master_votes', { round: 1, agent: 'agent-b', decision: 'approve', note: 'Historical approval' });
    for (const [name, expires_at] of [['expired', old], ['boundary', now], ['live', now + 400 * DAY]]) {
      insert('file_claims', { path: name, agent: 'agent-a', expires_at, created_at: old });
    }
    const tables = ['events', 'audit', 'messages', 'patches', 'tasks', 'news', 'news_details', 'questions',
      'huddles', 'huddle_posts', 'huddle_votes', 'dashboard_tasks', 'master_sections', 'master_edits',
      'master_state', 'master_votes'];
    const snapshot = () => Object.fromEntries(tables.map(table =>
      [table, db.prepare(`SELECT * FROM ${table} ORDER BY rowid`).all()]));
    const before = snapshot();
    for (let pass = 0; pass < 3; pass++) {
      if (pass === 2) room = new Room(ctx, env);
      const alarmStart = Date.now();
      await room.alarm();
      assert.deepEqual(snapshot(), before, 'all historical contents survive repeated alarms and restart');
      assert.deepEqual(db.prepare('SELECT path FROM file_claims ORDER BY path').all().map(r => r.path), ['live']);
      assert.ok(await ctx.storage.getAlarm() >= alarmStart + DAY, 'claim expiry alarm is rescheduled');
    }
    // Explicit actions still work after housekeeping, without removing task history.
    const task = await request('/v1/tasks', { title: 'Cancelable', description: 'Manual action', assignee: 'agent-b' }, 201);
    assert.equal((await request('/v1/tasks/' + task.id + '/cancel', {})).status, 'cancelled');
    assert.equal(db.prepare('SELECT status FROM tasks WHERE id=?').get(task.id).status, 'cancelled');
    const released = await request('/v1/claims/release', { path: 'live' });
    assert.equal(released.ok, true);
    assert.ok(released.changed > 0);
    assert.equal(db.prepare('SELECT COUNT(*) AS n FROM file_claims').get().n, 0);
  } finally { db.close(); }
};

if (require.main === module) module.exports().then(() => console.log('Worker alarms retain all history and expire only live claims'))
  .catch(error => { console.error(error); process.exitCode = 1; });
