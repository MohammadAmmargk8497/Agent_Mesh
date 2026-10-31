        // Home-server-only team knowledge; runs inside the existing member session.
        let homeHandoffs = [], homeNotes = [], noteQuery = "", noteTag = "";
        let homeFeatureCursor = null, homeNotesCachePath = "";
        const noteDeleteConfirm = new Set();

        function notesPath() {
          return "/api/notes?q=" + encodeURIComponent(noteQuery) + "&tag=" + encodeURIComponent(noteTag);
        }

        async function loadHomeFeatures(cursor) {
          if (!Number.isSafeInteger(cursor) || cursor < 0) throw new Error("Invalid team event cursor.");
          const path = notesPath(), changed = cursor !== homeFeatureCursor;
          const loadNotes = changed || path !== homeNotesCachePath;
          const [handoffs, notes] = await Promise.all([
            changed ? api("/api/handoffs") : null,
            loadNotes ? api(path) : null
          ]);
          if ((changed && !Array.isArray(handoffs?.handoffs)) || (loadNotes && !Array.isArray(notes?.notes))) {
            throw new Error("Invalid team knowledge snapshot.");
          }
          // Commit these caches only after refresh's revision and session guards.
          return { cursor, path, handoffs: changed ? handoffs.handoffs : homeHandoffs,
            notes: loadNotes ? notes.notes : homeNotes };
        }

        function homeField(id, label, maximum = 8000, required = true, singleLine = false) {
          const input = h(singleLine ? "input" : "textarea", {
            id, required, maxlength: maximum == null ? null : String(maximum), rows: "3", type: singleLine ? "text" : null,
            "data-focus": "home-field:" + id
          });
          input.value = drafts.get(id) || "";
          input.addEventListener("input", () => drafts.set(id, input.value));
          return h("label", { class: "field", for: id, text: label }, input);
        }

        function homeValue(id) { return get(id).value.trim(); }
        function homeLines(id) { return homeValue(id).split(/\r?\n/).map(line => line.trim()).filter(Boolean); }
        function homeList(label, values) {
          return values?.length ? h("div", { class: "details-body" }, h("h3", { text: label }),
            h("ul", {}, values.map(text => h("li", { text })))) : null;
        }

        function renderTaskHandoffs(task) {
          const rows = homeHandoffs.filter(row => row.task_key === task.key);
          const box = h("section", { class: "task-handoffs", "aria-label": "Task handoffs", "data-task-key": task.key },
            h("h4", { text: "Handoffs" }));
          for (const row of rows) box.append(h("article", { class: "item task-handoff", "data-handoff-id": row.id },
            h("p", { class: "meta" }, who(row.author_id), " · ", h("time", { text: clock(row.created_at) })),
            h("p", { class: "description", text: row.summary }),
            homeList("Changed files", row.changed_files), homeList("Open questions", row.open_questions),
            h("p", { class: "description", text: "Next step: " + row.next_step })));
          if (!rows.length) box.append(h("p", { class: "hint", text: "No handoffs yet." }));
          if (task.assignee_id === state.me.id || task.created_by === state.me.id) {
            const prefix = "home-handoff-" + task.key + "-";
            box.append(form(prefix + "form", "Leave a handoff", [
              homeField(prefix + "summary", "Summary"),
              homeField(prefix + "files", "Changed files (one per line)", null, false),
              homeField(prefix + "questions", "Open questions (one per line)", null, false),
              homeField(prefix + "next", "Next step")
            ], "Save handoff", async () => {
              await api("/api/handoff", { task_key: task.key, summary: homeValue(prefix + "summary"),
                changed_files: homeLines(prefix + "files"), open_questions: homeLines(prefix + "questions"),
                next_step: homeValue(prefix + "next") });
              for (const suffix of ["summary", "files", "questions", "next"]) drafts.delete(prefix + suffix);
            }));
          }
          return box;
        }

        function renderNotes() {
          get("team-notes-panel").hidden = false;
          preserveView("team-notes", renderNoteRows);
        }

        function renderNoteRows() {
          const box = clear(get("team-notes"));
          const search = homeField("home-notes-query", "Search notes", 8000, false, true);
          const tag = homeField("home-notes-tag", "Tag filter (exact tag)", null, false, true);
          const filter = form("home-notes-filter", "Find team notes", [search, tag], "Search", async () => {
            noteQuery = homeValue("home-notes-query"); noteTag = homeValue("home-notes-tag");
          });
          filter.append(h("button", { type: "button", text: "Clear filters", onclick: () => act(async () => {
            noteQuery = noteTag = ""; drafts.delete("home-notes-query"); drafts.delete("home-notes-tag");
          }) }));
          box.append(filter);
          const list = h("div", { class: "items", id: "home-notes-list" });
          for (const note of homeNotes) {
            const row = h("article", { class: "item team-note", "data-note-id": note.id },
              h("h3", { class: "item-title", text: note.title }),
              h("p", { class: "meta" }, who(note.author_id), " · ", h("time", { text: clock(note.created_at) })),
              h("p", { class: "description", text: note.body }),
              h("p", { class: "meta", text: (note.tags || []).map(tag => "#" + tag).join(" ") }));
            if (note.author_id === state.me.id) {
              const actions = h("div", { class: "write-actions" });
              if (noteDeleteConfirm.has(note.id)) actions.append(
                h("p", { text: "Permanently delete this note? This cannot be undone." }),
                h("button", { type: "button", text: "Confirm delete", "data-note-delete": note.id,
                  onclick: () => act(async () => {
                    await api("/api/notes/" + encodeURIComponent(note.id), undefined, "DELETE");
                    noteDeleteConfirm.delete(note.id);
                  }, "Note permanently deleted.") }),
                h("button", { type: "button", text: "Cancel deletion", onclick: () => {
                  noteDeleteConfirm.delete(note.id); renderNotes();
                } }));
              else actions.append(h("button", { type: "button", text: "Delete note", onclick: () => {
                noteDeleteConfirm.add(note.id); renderNotes();
              } }));
              row.append(actions);
            }
            list.append(row);
          }
          if (!homeNotes.length) list.append(h("p", { class: "empty", text: "No notes match these filters." }));
          get("team-notes-count").textContent = String(homeNotes.length);
          box.append(list, form("home-note-form", "Share a note", [
            homeField("home-note-title", "Title", 200, true, true),
            homeField("home-note-body", "Body"),
            homeField("home-note-tags", "Tags (comma-separated, up to 10)", null, false, true)
          ], "Save note", async () => {
            const tags = homeValue("home-note-tags").split(",").map(tag => tag.trim()).filter(Boolean);
            if (tags.length > 10) throw new Error("Use at most 10 tags.");
            await api("/api/notes", { title: homeValue("home-note-title"), body: homeValue("home-note-body"), tags });
            for (const id of ["home-note-title", "home-note-body", "home-note-tags"]) drafts.delete(id);
          }));
        }

        function resetHomeFeatures() {
          homeHandoffs = []; homeNotes = []; noteQuery = noteTag = ""; noteDeleteConfirm.clear();
          homeFeatureCursor = null; homeNotesCachePath = "";
          for (const node of get("tasks").querySelectorAll(".task-handoffs")) node.remove();
          get("team-notes-panel").hidden = true;
          get("team-notes").replaceChildren();
          get("team-notes-count").textContent = "0";
        }
