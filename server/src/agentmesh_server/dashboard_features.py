"""Compose home-server features without changing the shared Worker dashboard.

The package's dashboard.html remains the shared upstream baseline. These small,
checked insertion points add server-only behavior inside its existing member
session, so requests share the same authentication and cancellation lifecycle.
"""

from importlib.resources import files


def _replace_once(page, anchor, replacement):
    if page.count(anchor) != 1:
        raise ValueError("home dashboard extension anchor changed: " + anchor[:80])
    return page.replace(anchor, replacement, 1)


def compose_dashboard(page):
    """Add team notes and task handoffs to the canonical dashboard template."""
    extension = files("agentmesh_server").joinpath("dashboard_features.js").read_text(encoding="utf-8")
    panel = '''        <section id="team-notes-panel" class="panel panel-notes" aria-labelledby="team-notes-title" hidden>
          <div class="panel-heading"><div><div class="panel-label">Shared team knowledge</div><h2 id="team-notes-title">Notes</h2></div><span id="team-notes-count" class="count">0</span></div>
          <div id="team-notes" aria-label="Shared team notes"></div>
        </section>
'''
    page = _replace_once(page, '      </div>\n      <aside class="col col-feed"', panel + '      </div>\n      <aside class="col col-feed"')
    page = _replace_once(page, '  </style>',
                         '    .panel-notes{order:12}.task-handoffs{border-top:1px solid var(--line-soft);margin-top:12px;padding-top:8px}\n'
                         '    .task-handoffs h4{font-size:12px;margin:4px 0}.task-handoffs .item{padding:10px 0}\n  </style>')
    page = _replace_once(page, '        function takeToken() {', extension + '\n        function takeToken() {')
    page = _replace_once(page, '        async function api(path, body) {',
                         '        async function api(path, body, method = body === undefined ? "GET" : "POST") {')
    page = _replace_once(page, '            const options = { headers: { Authorization: `Bearer ${token}` }, cache: "no-store", signal: abort.signal };',
                         '            const options = { method, headers: { Authorization: `Bearer ${token}` }, cache: "no-store", signal: abort.signal };')
    page = _replace_once(page, '              options.method = "POST";\n', '')
    page = _replace_once(page, "            buttons.length ? h('div', { class: 'card-actions' }, buttons) : null,\n          );\n        }\n\n        // ---------- huddles ----------",
                         "            buttons.length ? h('div', { class: 'card-actions' }, buttons) : null,\n            renderTaskHandoffs(t),\n          );\n        }\n\n        // ---------- huddles ----------")
    page = _replace_once(page, '          renderFeed(); renderQuestions(); renderClaims(); renderAsks();',
                         '          renderFeed(); renderQuestions(); renderClaims(); renderAsks(); renderNotes();')
    page = _replace_once(page, '          switch (ev.kind) {', '''          switch (ev.kind) {
            case 'handoff_created': return [who(actor), ' left a handoff for ', taskRef(d.task_key), ': ', shorten(d.summary, 200)];
            case 'note_created': return [who(actor), ' shared a note: ', quoted(d.title || '')];
            case 'note_deleted': return [who(actor), ' permanently deleted a note: ', quoted(d.title || '')];''')
    page = _replace_once(page, '          state = next;\n          renderAll();',
                         '          const home = await loadHomeFeatures(next.last_event_id);\n'
                         '          if (revision !== refreshRevision) return;\n'
                         '          state = next;\n'
                         '          homeHandoffs = home.handoffs; homeNotes = home.notes;\n'
                         '          homeFeatureCursor = home.cursor; homeNotesCachePath = home.path;\n'
                         '          renderAll();')
    page = _replace_once(page, '        function stop() {\n', '        function stop() {\n          resetHomeFeatures();\n')
    return page
