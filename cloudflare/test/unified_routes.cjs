// Unified dashboard routing, all-message transcript and scoped legacy adapters.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { load, context, root } = require('./runtime.cjs');

module.exports = async function testUnifiedRoutes() {
  const worker = load(path.join(root, 'src/index.ts'));
  const env = { AGENT_A_TOKEN: 'adapter-a'.repeat(5), AGENT_B_TOKEN: 'adapter-b'.repeat(5), VIEWER_TOKEN: 'adapter-v'.repeat(5) };
  const legacy = context(), database = context();
  const room = new worker.Room(legacy.ctx, env), hub = new worker.HubRoom(database.ctx, env);
  env.ROOM = { idFromName: name => name, get: () => ({ fetch: request => room.fetch(request) }) };
  env.HUB = { idFromName: name => name, get: () => ({
    fetch: request => hub.fetch(request), teamTranscript: teamId => hub.teamTranscript(teamId),
    legacyTeamId: () => hub.legacyTeamId(),
  }) };
  async function request(route, token, method = 'GET', data, host = 'mesh.example', extra = {}) {
    return worker.default.fetch(new Request('https://' + host + route, {
      method, headers: { ...(token ? { authorization: 'Bearer ' + token } : {}), ...extra },
      ...(data === undefined ? {} : { body: JSON.stringify(data) }),
    }), env);
  }
  async function value(route, token = env.AGENT_A_TOKEN, method = 'GET', data, status = 200) {
    const response = await request(route, token, method, data);
    assert.equal(response.status, status, route + ': ' + await response.clone().text());
    return response.json();
  }
  try {
    const initial = await value('/api/state');
    const team = initial.team.id;
    const invite = await value('/api/invite', env.AGENT_A_TOKEN, 'POST', {});
    const joined = await value('/api/join', null, 'POST', { code: invite.invite_url, member_name: 'Observer' });
    const other = await value('/api/teams', null, 'POST', { team_name: 'default', member_name: 'agent-a' });
    const otherMember = await value('/api/join', null, 'POST', { code: other.invite_url, member_name: 'Other member' });
    const hiddenAsk = await value('/api/ask', other.token, 'POST', { to: 'Other member', question: 'Other team question', wait_s: 0 });
    await value('/api/reply', otherMember.token, 'POST', { ask_id: hiddenAsk.ask_id, answer: 'Other team answer' });
    const hiddenHuddle = await value('/api/huddle/start', other.token, 'POST', { topic: 'Other team huddle' });
    await value('/api/huddle/post', otherMember.token, 'POST', { id: hiddenHuddle.huddle.id, text: 'Other team comment' });
    await value('/api/post', other.token, 'POST', { kind: 'update', text: 'Other team post' });
    const sent = await value('/v1/messages', env.AGENT_A_TOKEN, 'POST', { to: 'agent-b', text: 'default-team-only' }, 201);
    const patch = await value('/v1/patches', env.AGENT_A_TOKEN, 'POST', { title: 'Private patch', text: 'private diff' }, 201);
    const privateTask = await value('/v1/tasks', env.AGENT_B_TOKEN, 'POST', {
      title: 'Agent-b task', description: 'Full team transcript coverage', assignee: 'agent-b',
    }, 201);
    const privateMessage = '00000000-0000-4000-8000-000000000001';
    legacy.db.prepare("INSERT INTO messages (id,sender,recipient,text,created_at) VALUES (?,'agent-b','agent-b','Team transcript note',?)")
      .run(privateMessage, Date.now());
    const ask = await value('/api/ask', env.AGENT_A_TOKEN, 'POST', { to: 'agent-b', question: 'Review the transcript?', wait_s: 0 });
    await value('/api/reply', env.AGENT_B_TOKEN, 'POST', { ask_id: ask.ask_id, answer: 'First answer is retained' });
    await value('/api/reply', env.AGENT_B_TOKEN, 'POST', { ask_id: ask.ask_id, answer: 'Approved transcript' });
    const post = await value('/api/post', env.AGENT_A_TOKEN, 'POST', { kind: 'update', text: 'Human-readable team post' });
    const discussion = await value('/api/huddle/start', env.AGENT_A_TOKEN, 'POST', { topic: 'Transcript plan' });
    const idea = await value('/api/huddle/post', env.AGENT_A_TOKEN, 'POST', {
      id: discussion.huddle.id, kind: 'idea', text: 'Full huddle post ' + 'x'.repeat(550),
    });
    const comment = await value('/api/huddle/post', env.AGENT_B_TOKEN, 'POST', {
      id: discussion.huddle.id, text: 'Keep every message for humans', on: idea.seq,
    });
    // Deliberately interleave the stores using realistic epoch millisecond
    // timestamps, rather than relying on operations landing in different ms.
    const at = Date.now() - 10_000;
    legacy.db.prepare('UPDATE messages SET created_at=? WHERE id=?').run(at, sent.id);
    legacy.db.prepare('UPDATE messages SET created_at=? WHERE id=?').run(at - 1, privateMessage);
    database.db.prepare('UPDATE asks SET created_at=?, answered_at=? WHERE team_id=? AND id=?')
      .run(at + 1000, at + 3000, team, ask.ask_id);
    database.db.prepare("UPDATE events SET created_at=? WHERE team_id=? AND kind='ask'")
      .run(at + 1000, team);
    database.db.prepare("UPDATE events SET created_at=? WHERE team_id=? AND kind='reply' AND json_extract(data,'$.answer')=?")
      .run(at + 2000, team, 'First answer is retained');
    database.db.prepare("UPDATE events SET created_at=? WHERE team_id=? AND kind='reply' AND json_extract(data,'$.answer')=?")
      .run(at + 3000, team, 'Approved transcript');
    database.db.prepare('UPDATE events SET created_at=? WHERE id=?').run(at + 4000, post.event_id);
    database.db.prepare('UPDATE huddle_posts SET created_at=? WHERE team_id=? AND huddle_id=? AND seq=?')
      .run(at + 5000, team, discussion.huddle.id, idea.seq);
    database.db.prepare('UPDATE huddle_posts SET created_at=? WHERE team_id=? AND huddle_id=? AND seq=?')
      .run(at + 6000, team, discussion.huddle.id, comment.seq);
    const agentSnapshot = await value('/v1/dashboard/state');
    assert.ok(!agentSnapshot.tasks.some(row => row.id === privateTask.id));
    assert.ok(!agentSnapshot.messages.some(row => row.id === privateMessage));
    const route = '/api/legacy/transcript?team=' + team;
    // A generated member key, not just the environment agent token, can read the default team's panels.
    const snapshot = await value(route, joined.token);
    // A cold boundary can also resolve a generated member without any preceding
    // seeded-agent request. The binding must then stay cached on that environment.
    const coldEnv = { ...env };
    const coldRead = () => worker.default.fetch(new Request('https://mesh.example' + route, {
      headers: { authorization: 'Bearer ' + joined.token },
    }), coldEnv);
    database.db.prepare('UPDATE members SET last_seen=1 WHERE team_id=? AND name IN (?,?)').run(team, 'agent-a', 'agent-b');
    assert.equal((await coldRead()).status, 200);
    assert.deepEqual(database.db.prepare('SELECT last_seen FROM members WHERE team_id=? AND name IN (?,?)').all(team, 'agent-a', 'agent-b').map(row => row.last_seen), [1, 1], 'cold member transcript reads do not fabricate seeded-agent presence');
    assert.ok(snapshot.messages.some(row => row.id === sent.id));
    assert.ok(snapshot.patches.some(row => row.id === patch.id));
    assert.ok(snapshot.tasks.some(row => row.id === privateTask.id), 'member adapter retains the full team task snapshot');
    assert.ok(snapshot.messages.some(row => row.id === privateMessage), 'member adapter retains the full team transcript');
    assert.deepEqual(snapshot.messages.map(row => row.kind), [
      'direct', 'direct', 'ask', 'answer', 'answer', 'huddle post', 'huddle post', 'huddle comment',
    ]);
    assert.deepEqual(snapshot.messages.slice(1).map(row => row.text), [
      'default-team-only', 'Review the transcript?', 'First answer is retained', 'Approved transcript',
      'Human-readable team post', 'Full huddle post ' + 'x'.repeat(550), 'Keep every message for humans',
    ]);
    assert.ok(snapshot.messages.every((row, index, rows) => !index || row.created_at >= rows[index - 1].created_at));
    assert.deepEqual(snapshot.messages.filter(row => row.ask_id).map(row => [row.ask_id, row.sender, row.recipient]), [
      [ask.ask_id, 'agent-a', 'agent-b'], [ask.ask_id, 'agent-b', 'agent-a'], [ask.ask_id, 'agent-b', 'agent-a'],
    ]);
    assert.equal(snapshot.messages.at(-1).huddle_id, discussion.huddle.id);
    assert.equal(snapshot.messages.at(-1).recipient, 'team');
    assert.ok(snapshot.messages.every(row => !row.text.startsWith('Other team')));
    assert.ok(snapshot.audit.length);
    assert.equal((await value('/api/legacy/patches?team=' + team, joined.token)).patches[0].id, patch.id);
    assert.equal((await value('/api/legacy/patches/' + patch.id + '?team=' + team, joined.token)).patch.text, 'private diff');
    assert.equal((await request(route)).status, 401);
    assert.equal((await request(route, env.VIEWER_TOKEN)).status, 401);
    assert.equal((await request('/api/legacy/transcript', joined.token)).status, 403);
    assert.equal((await request('/api/legacy/transcript?team=' + other.team_id, joined.token)).status, 403);
    // Even a team named default, with a member named agent-a, cannot enter the
    // seeded team's room. Its own transcript contains only its hub messages.
    const otherTranscript = await value('/api/legacy/transcript?team=' + other.team_id, other.token);
    for (const after of ['not-an-integer', '1.5', '', '1e3', '9007199254740992']) {
      assert.equal((await request('/api/legacy/transcript?team=' + other.team_id + '&after=' + after, other.token)).status, 400);
    }
    assert.deepEqual(otherTranscript.messages.map(row => row.text).sort(), [
      'Other team answer', 'Other team comment', 'Other team post', 'Other team question',
    ]);
    assert.deepEqual(otherTranscript.tasks, []);
    assert.deepEqual(otherTranscript.patches, []);
    for (const suffix of ['/patches', '/patches/' + patch.id]) {
      assert.equal((await request('/api/legacy' + suffix + '?team=' + other.team_id, other.token)).status, 403);
    }
    for (const suffix of ['/transcript', '/patches', '/patches/' + patch.id]) {
      assert.equal((await request('/api/legacy' + suffix + '?team=' + team, other.token, 'GET', undefined,
        'mesh.example', { 'x-agentmesh-agent': 'viewer' })).status, 403);
    }
    assert.equal((await request(route, joined.token, 'POST', {})).status, 405);
    assert.equal((await request('/api/legacy/tasks?team=' + team, joined.token)).status, 404);
    assert.equal((await request('/v1/transcript', env.AGENT_A_TOKEN)).status, 403);
    for (const route of ['/v1/transcript', '/v1/whoami', '/v1/dashboard/state', '/api/state', '/api/legacy/transcript?team=' + team]) {
      assert.equal((await request(route, env.VIEWER_TOKEN)).status, 401, 'former viewer key rejected: ' + route);
    }
    assert.equal((await request('/v1/messages', env.VIEWER_TOKEN, 'POST', { to: 'agent-b', text: 'not accepted' })).status, 401);
    for (const token of [env.AGENT_A_TOKEN, env.AGENT_B_TOKEN, env.VIEWER_TOKEN, joined.token]) {
      const expected = token === env.AGENT_A_TOKEN || token === env.AGENT_B_TOKEN ? 404 : 401;
      assert.equal((await request('/internal/transcript', token, 'GET', undefined,
        'mesh.example', { 'x-agentmesh-agent': 'legacy-adapter' })).status, expected,
        'internal snapshot cannot be reached by spoofing a context header');
    }
    assert.equal((await room.fetch(new Request('https://mesh.example/v1/transcript', {
      headers: { 'x-agentmesh-agent': 'viewer' },
    }))).status, 401, 'the room does not recognize a viewer identity');
    // Once bound, adapter refreshes must not authenticate as the seeded agent and fake its presence.
    database.db.prepare('UPDATE members SET last_seen=1 WHERE name=? AND team_id=?').run('agent-a', team);
    await value(route, joined.token);
    assert.equal((await coldRead()).status, 200);
    assert.equal(database.db.prepare('SELECT last_seen FROM members WHERE name=? AND team_id=?').get('agent-a', team).last_seen, 1);
    const edited = await value('/api/prompt/edit', env.AGENT_A_TOKEN, 'POST', {
      section: 'Idea', text: 'A real master-prompt message',
    });
    await value('/api/prompt/submit', env.AGENT_A_TOKEN, 'POST', {});
    await value('/api/prompt/vote', env.AGENT_B_TOKEN, 'POST', { approve: true, comment: 'A real prompt review comment' });
    const planned = await value('/api/plan', env.AGENT_A_TOKEN, 'POST', { tasks: [{ title: 'Transcript test' }] });
    const taskId = planned.tasks[0].key;
    await value('/api/task', env.AGENT_A_TOKEN, 'POST', { action: 'take', key: taskId });
    await value('/api/task', env.AGENT_A_TOKEN, 'POST', { action: 'update', key: taskId, note: 'A real task note' });
    const notes = (await value(route, joined.token)).messages;
    assert.ok(notes.some(row => row.kind === 'master-prompt' && row.text === 'A real master-prompt message'
      && row.prompt_version === edited.prompt.version));
    assert.ok(notes.some(row => row.kind === 'master-prompt' && row.text === 'A real prompt review comment'));
    assert.ok(notes.some(row => row.kind === 'task note' && row.task_id === taskId && row.text === 'A real task note'));
    assert.ok(!notes.some(row => ['prompt_submitted', 'task_assigned'].includes(row.kind)), 'status events are not invented messages');
    // Older restored asks can lack events. Project their stored answer once,
    // without writing to the hub or inventing a new event.
    const fromId = initial.me.id;
    const toId = initial.members.find(row => row.name === 'agent-b').id;
    database.db.prepare('INSERT INTO asks(team_id,id,from_id,to_id,question,answer,created_at,answered_at) VALUES (?,?,?,?,?,?,?,?)')
      .run(team, 'Q999', fromId, toId, 'Restored question', 'Restored answer', at + 100, at + 200);
    database.db.prepare('INSERT INTO asks(team_id,id,from_id,to_id,question,answer,created_at,answered_at) VALUES (?,?,?,?,?,?,?,?)')
      .run(team, 'Q998', fromId, toId, 'Tied restored question', 'Tied restored answer', at + 100, at + 100);
    const beforeRead = database.db.prepare('SELECT total_changes() AS n').get().n;
    const projected = await hub.teamTranscript(team);
    assert.equal(database.db.prepare('SELECT total_changes() AS n').get().n, beforeRead, 'transcript projection is read-only');
    assert.deepEqual(projected.filter(row => row.ask_id === 'Q999').map(row => row.kind), ['ask', 'answer']);
    assert.deepEqual(projected.filter(row => row.ask_id === 'Q998').map(row => row.kind), ['ask', 'answer']);
    // Bound the merged list, not just each individual source. Retain the
    // newest hub message after 500 direct messages and discard older entries.
    const insertMessage = legacy.db.prepare("INSERT INTO messages (id,sender,recipient,text,created_at) VALUES (?,'agent-a','agent-b',?,?)");
    for (let i = 0; i < 500; i++) insertMessage.run('bulk-' + i, 'Direct ' + i, at + 20_000 + i);
    const newest = await value('/api/post', env.AGENT_B_TOKEN, 'POST', { kind: 'decision', text: 'Newest team message' });
    database.db.prepare('UPDATE events SET created_at=? WHERE id=?').run(at + 30_000, newest.event_id);
    const bounded = (await value(route, joined.token)).messages;
    assert.equal(bounded.length, 500);
    assert.equal(bounded[0].text, 'Direct 1');
    assert.equal(bounded.at(-1).text, 'Newest team message');
    assert.equal(bounded.at(-1).sender, 'agent-b');
    // Equal-time direct messages keep insertion order, not random UUID order.
    // The limit still discards the oldest insertion at the cutoff.
    for (let i = 0; i < 501; i++) insertMessage.run('tied-' + (501 - i), 'Tied direct ' + i, at + 40_000);
    const tied = (await value(route, joined.token)).messages;
    assert.equal(tied.length, 500);
    assert.equal(tied[0].text, 'Tied direct 1');
    assert.equal(tied.at(-1).text, 'Tied direct 500');
    const dashboard = fs.readFileSync(path.join(root, 'src/dashboard.html'), 'utf8');
    for (const [host, route, template] of [
      ['mesh.example', '/t/' + team, dashboard], ['agentmesh.zahid.win', '/t/' + team, dashboard],
      ['agentmesh.zahid.win', '/', dashboard],
    ]) {
      const response = await request(route, null, 'GET', undefined, host);
      assert.equal(response.status, 200);
      const html = await response.text();
      const nonce = /script-src 'nonce-([^']+)'/.exec(response.headers.get('content-security-policy'))[1];
      assert.equal(html, template.replaceAll('__AGENTMESH_NONCE__', nonce));
      assert.ok(!/OpenHuddle/i.test(html));
      for (const token of [env.AGENT_A_TOKEN, env.AGENT_B_TOKEN, joined.token, other.token]) assert.ok(!html.includes(token));
      const head = await request(route, null, 'HEAD', undefined, host);
      assert.equal(head.status, 200);
      assert.equal(await head.text(), '');
    }
    for (const route of ['/viewer', '/index.html']) {
      for (const method of ['GET', 'HEAD']) {
        const response = await request(route, null, method, undefined, 'agentmesh.zahid.win');
        assert.equal(response.status, 302);
        assert.equal(response.headers.get('location'), '/');
        assert.equal(await response.text(), '');
      }
    }
    await value('/api/members/remove', env.AGENT_A_TOKEN, 'POST', { member: joined.member_id });
    assert.equal((await request(route, joined.token)).status, 401, 'revocation takes effect for adapters too');
  } finally { legacy.db.close(); database.db.close(); }
};

if (require.main === module) module.exports().then(() => console.log('Unified Worker routes and default-team adapter isolation passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
