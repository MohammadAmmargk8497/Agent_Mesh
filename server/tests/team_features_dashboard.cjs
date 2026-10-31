// The real served scripts and DOM, real local HTTP writes, and no browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { browser, parseDocument, idle, login, poll, click, set, findButton, postFrom } =
  require(path.resolve(__dirname, '../../cloudflare/test/unified_dashboard.cjs'));

async function run(payload) {
  const origin = new URL(payload.origin);
  assert.equal(origin.hostname, '127.0.0.1', 'the harness must only call its disposable loopback server');
  const request = (route, options) => fetch(new URL(route, origin), options);
  async function api(route, body, token, method = body === undefined ? 'GET' : 'POST') {
    const response = await request(route, {
      method, headers: { ...(token ? { Authorization: 'Bearer ' + token } : {}), 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const data = await response.json();
    assert.equal(response.status, 200, route + ': local API request succeeded');
    return data;
  }
  const owner = await api('/api/teams', { team_name: 'Feature dashboard team', member_name: 'Note author' });
  const invite = await api('/api/invite', {}, owner.token);
  const assignee = await api('/api/join', { code: invite.invite_url, member_name: 'Task assignee' });
  const reader = await api('/api/join', { code: invite.invite_url, member_name: 'Team reader' });
  const other = await api('/api/teams', { team_name: 'Other team', member_name: 'Other author' });
  await api('/api/plan', { tasks: [{ key: 'T1', title: 'Handoff target' }, { key: 'T2', title: 'Separate task' }] }, owner.token);
  await api('/api/task', { action: 'take', key: 'T1' }, assignee.token);
  const malicious = '<img src=x onerror=alert(1)><script>bad()</script>';
  const handoff = (task_key, summary) => ({ task_key, summary, changed_files: ['server/example.py', malicious],
    open_questions: ['Question ' + malicious], next_step: 'Next ' + malicious });
  const older = (await api('/api/handoff', handoff('T1', 'Older handoff'), owner.token)).handoff;
  const newer = (await api('/api/handoff', handoff('T1', 'Newest ' + malicious), assignee.token)).handoff;
  const separate = (await api('/api/handoff', handoff('T2', 'Separate handoff'), owner.token)).handoff;
  const matching = (await api('/api/notes', { title: 'Release design ' + malicious, body: 'Visible note body ' + malicious, tags: ['release', 'planning'] }, owner.token)).note;
  const peerNote = (await api('/api/notes', { title: 'Peer note', body: 'Only its author can delete this', tags: ['release'] }, assignee.token)).note;
  await api('/api/notes', { title: 'Design only', body: 'Different tag', tags: ['design'] }, owner.token);
  await api('/api/notes', { title: 'Other team secret', body: 'Must not appear', tags: ['release'] }, other.token);

  async function page(route) {
    const response = await request(route);
    assert.equal(response.status, 200);
    const html = await response.text();
    for (const member of [owner, assignee, reader, other]) assert.ok(!html.includes(member.token), 'no runtime member token is embedded in HTML');
    return { tree: parseDocument(html), team: owner.team_id, origin: payload.origin,
      scripts: [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)].map(match => match[1]) };
  }
  const input = await page('/t/' + owner.team_id);
  const rootInput = await page('/');
  const app = browser(input, request, { hash: '#k=' + encodeURIComponent(owner.token) });
  await idle(app);
  assert.equal(app.get('auth').hidden, true, 'member fragment opens team dashboard');
  assert.equal(app.get('team-notes-panel').hidden, false, 'member sees Notes panel');
  assert.ok(!app.get('team-notes').textContent.includes('Other team secret'), 'other-team notes are absent');
  for (const call of app.calls.filter(call => !call.url.startsWith('/auth/'))) {
    assert.equal(new Headers(call.options.headers).has('Authorization'), false, 'page never handles bearer credentials');
    assert.equal(call.networkHeaders.get('Cookie'), 'agentmesh_session=' + owner.token, 'feature reads use the HttpOnly session at the transport boundary');
  }

  const cards = app.get('tasks').querySelectorAll('.task-handoffs');
  const target = cards.find(node => node.dataset.taskKey === 'T1');
  const separateCard = cards.find(node => node.dataset.taskKey === 'T2');
  assert.deepEqual(target.querySelectorAll('.task-handoff').map(row => Number(row.dataset.handoffId)), [newer.id, older.id], 'newest handoff is rendered first');
  assert.deepEqual(separateCard.querySelectorAll('.task-handoff').map(row => Number(row.dataset.handoffId)), [separate.id], 'handoffs stay on their task card');
  assert.ok(target.textContent.includes(malicious), 'handoff text is rendered literally');
  assert.ok(app.get('team-notes').textContent.includes(malicious), 'note text is rendered literally');
  assert.equal(app.get('workspace').querySelectorAll('img,script').length, 0, 'untrusted content never inserts executable DOM');
  assert.ok(app.get('home-handoff-T1-form'), 'creator has handoff form even when assigned to someone else');
  assert.equal(app.get('home-note-title').getAttribute('maxlength'), '200');
  assert.equal(app.get('home-note-body').getAttribute('maxlength'), '8000');

  // Both handoff and note drafts survive the normal background refresh.
  const draft = await set(app, 'home-handoff-T1-summary', 'Dashboard handoff draft');
  draft.focus(); draft.setSelectionRange(3, 9);
  await set(app, 'home-note-title', 'Unsaved title');
  await set(app, 'home-note-body', 'Unsaved body');
  const idleBefore = app.calls.length;
  await poll(app);
  const idleCalls = app.calls.slice(idleBefore);
  assert.ok(idleCalls.some(call => call.url === '/api/state'), 'idle polling still checks the team event cursor');
  assert.ok(!idleCalls.some(call => call.url === '/api/handoffs' || call.url.startsWith('/api/notes?')), 'unchanged idle poll reuses permanent history without downloading it');
  assert.equal(app.get('home-handoff-T1-summary').value, 'Dashboard handoff draft');
  assert.equal(app.get('home-note-title').value, 'Unsaved title');
  assert.equal(app.get('home-note-body').value, 'Unsaved body');
  assert.equal(app.document.activeElement, app.get('home-handoff-T1-summary'), 'handoff draft keeps focus');
  assert.equal(app.document.activeElement.selectionStart, 3);
  await set(app, 'home-handoff-T1-files', 'server/one.py\nserver/two.py');
  await set(app, 'home-handoff-T1-questions', 'What remains?\nWhich release?');
  await set(app, 'home-handoff-T1-next', 'Run final tests');
  let before = app.calls.length;
  await app.get('home-handoff-T1-form').fire('submit'); await idle(app);
  const savedHandoff = postFrom(app.calls.slice(before), '/api/handoff');
  assert.deepEqual(savedHandoff.changed_files, ['server/one.py', 'server/two.py']);
  assert.deepEqual(savedHandoff.open_questions, ['What remains?', 'Which release?']);
  const handoffs = (await api('/api/handoffs?task_key=T1', undefined, reader.token)).handoffs;
  assert.equal(handoffs[0].summary, 'Dashboard handoff draft', 'UI handoff is readable by a team member');
  assert.ok(app.get('tasks').textContent.includes('Dashboard handoff draft'));
  assert.ok(app.get('news').textContent.includes('left a handoff for T1'), 'handoff feed has task context');

  await set(app, 'home-note-title', 'Created from the Notes panel');
  await set(app, 'home-note-body', 'Rendered body ' + malicious);
  await set(app, 'home-note-tags', 'panel, release');
  before = app.calls.length;
  await app.get('home-note-form').fire('submit'); await idle(app);
  const savedNote = postFrom(app.calls.slice(before), '/api/notes');
  assert.deepEqual(savedNote.tags, ['panel', 'release']);
  assert.ok(app.get('home-notes-list').textContent.includes(savedNote.title));
  assert.ok(app.get('news').textContent.includes(savedNote.title), 'note creation is visible in the feed');
  assert.equal(app.get('home-note-title').value, '', 'successful note creation clears its draft');
  const created = (await api('/api/notes?q=Created%20from&tag=panel', undefined, reader.token)).notes;
  assert.equal(created.length, 1, 'team reader can retrieve the UI-created note');

  await set(app, 'home-notes-query', 'design');
  await set(app, 'home-notes-tag', 'release');
  before = app.calls.length;
  await app.get('home-notes-filter').fire('submit'); await idle(app);
  assert.ok(app.calls.slice(before).some(call => call.url === '/api/notes?q=design&tag=release'), 'search and exact tag filter are sent together');
  assert.ok(!app.calls.slice(before).some(call => call.url === '/api/handoffs'), 'filter-only change reuses the handoff cache');
  assert.deepEqual(app.get('home-notes-list').querySelectorAll('.team-note').map(row => Number(row.dataset.noteId)), [matching.id]);
  await click(app, findButton(app.get('team-notes'), 'Clear filters'));
  assert.equal(app.get('home-notes-query').value, ''); assert.equal(app.get('home-notes-tag').value, '');

  const noteRow = id => app.get('home-notes-list').querySelectorAll('.team-note').find(row => Number(row.dataset.noteId) === id);
  assert.ok(!noteRow(peerNote.id).querySelectorAll('button').length, 'owner cannot delete a peer note');
  const deletes = () => app.calls.filter(call => call.options.method === 'DELETE').length;
  const count = deletes();
  await click(app, findButton(noteRow(created[0].id), 'Delete note'));
  assert.equal(deletes(), count, 'initial delete click never sends a DELETE');
  await click(app, findButton(noteRow(created[0].id), 'Cancel deletion'));
  assert.equal(deletes(), count, 'cancelling deletion does not send a DELETE');
  assert.ok((await api('/api/notes', undefined, owner.token)).notes.some(note => note.id === created[0].id));
  await click(app, findButton(noteRow(created[0].id), 'Delete note'));
  const deletion = await click(app, findButton(noteRow(created[0].id), 'Confirm delete'));
  assert.equal(deletes(), count + 1, 'only explicit confirmation sends one DELETE');
  assert.ok(deletion.some(call => call.url === '/api/notes/' + created[0].id && call.options.method === 'DELETE' && call.status === 200));
  assert.ok(!noteRow(created[0].id), 'confirmed note disappears from the panel');
  assert.ok(!(await api('/api/notes', undefined, reader.token)).notes.some(note => note.id === created[0].id));

  const peer = browser(input, request, { hash: '#k=' + encodeURIComponent(reader.token) }); await idle(peer);
  assert.ok(peer.get('tasks').textContent.includes('Dashboard handoff draft'), 'any team member reads handoffs');
  assert.equal(peer.get('tasks').querySelectorAll('.task-handoffs form').length, 0, 'unassigned noncreator has no handoff form');
  assert.equal(peer.get('home-notes-list').querySelectorAll('button').length, 0, 'reader cannot delete another author note');
  const assigned = browser(input, request, { hash: '#k=' + encodeURIComponent(assignee.token) }); await idle(assigned);
  assert.ok(assigned.get('home-handoff-T1-form'), 'assignee has handoff form');
  assert.equal(assigned.get('home-handoff-T2-form'), null, 'assignee cannot write another task handoff');

  const root = browser(rootInput, request, { pathname: '/', hash: '#k=' + encodeURIComponent(owner.token) }); await idle(root);
  assert.equal(root.get('team-notes-panel').hidden, false, 'root page renders member features too');
  assert.ok(root.get('tasks').textContent.includes('Dashboard handoff draft'));
  const legacy = browser(rootInput, request, { pathname: '/' }); await login(legacy, payload.legacy_token);
  assert.equal(legacy.get('team-notes-panel').hidden, true, 'legacy session does not expose team feature panel');
  assert.ok(!legacy.calls.some(call => call.url.startsWith('/api/notes') || call.url.startsWith('/api/handoffs')), 'legacy mode never loads team features');
  const wrongTeam = browser(input, request, { hash: '#k=' + encodeURIComponent(other.token) }); await idle(wrongTeam);
  assert.equal(wrongTeam.get('auth').hidden, false, 'wrong-team member key is rejected at a team URL');
  assert.equal(wrongTeam.get('team-notes-panel').hidden, true);
  assert.equal(wrongTeam.get('team-notes').textContent, '', 'wrong-team notes never render');
  assert.equal(wrongTeam.get('tasks').querySelectorAll('.task-handoffs').length, 0);

  // A transport may deliver an already-read response even after abort. First
  // prime the cache, then change the real event cursor and hold that notes read.
  let release, announce;
  const gate = new Promise(resolve => { release = resolve; });
  const entered = new Promise(resolve => { announce = resolve; });
  let capturedSignal, holdNotes = false;
  const stale = browser(input, async (route, options) => {
    const response = await request(route, { ...options, signal: undefined });
    if (holdNotes && route.startsWith('/api/notes?')) { capturedSignal = options.signal; announce(); await gate; }
    return response;
  }, { hash: '#k=' + encodeURIComponent(owner.token) });
  await idle(stale);
  await api('/api/handoff', handoff('T1', 'Refresh after cached history'), owner.token);
  holdNotes = true;
  const pendingPoll = poll(stale);
  await entered;
  await stale.get('sign-out').click();
  assert.equal(capturedSignal.aborted, true, 'signout aborts an in-flight feature request');
  release(); await pendingPoll;
  assert.equal(stale.get('auth').hidden, false);
  assert.equal(stale.get('team-notes-panel').hidden, true);
  assert.equal(stale.get('team-notes').textContent, '');
  assert.equal(stale.get('tasks').querySelectorAll('.task-handoffs').length, 0);
  assert.equal(stale.timers.size, 0);
  assert.equal(stale.sessionStorage.has('agentmesh:session'), false);
  for (const instance of [app, peer, assigned, root, legacy]) {
    await instance.get('sign-out').click(); await idle(instance);
    assert.equal(instance.get('team-notes-panel').hidden, true);
    assert.equal(instance.get('team-notes').textContent, '');
    assert.equal(instance.get('tasks').querySelectorAll('.task-handoffs').length, 0, 'signout removes old feature cards');
  }
  console.log('team-features-dashboard-ok');
}

run(JSON.parse(fs.readFileSync(0, 'utf8'))).catch(error => {
  console.error(error); process.exitCode = 1;
});
