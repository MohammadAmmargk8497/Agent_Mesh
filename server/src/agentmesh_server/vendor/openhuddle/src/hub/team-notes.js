// AgentMesh home-server extension; the Worker intentionally has no corresponding routes.
import { badRequest, forbidden, notFound } from './errors.js';
import { cleanText, parse } from './text.js';

function text(value, field, max, required = false) {
  if (value === undefined && !required) return '';
  if (typeof value !== 'string' || value.length > max) {
    throw badRequest('invalid_text', `${field} must be a string of at most ${max} characters.`);
  }
  const cleaned = cleanText(value, max);
  if (required && !cleaned) throw badRequest('missing_text', `${field} is required.`);
  return cleaned;
}

function strings(value, field, maxItems = Infinity) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > maxItems || value.some((item) => typeof item !== 'string')) {
    throw badRequest('invalid_list', `${field} must be an array of strings${Number.isFinite(maxItems) ? ` (at most ${maxItems})` : ''}.`);
  }
  return value.map((item) => cleanText(item, item.length)).filter(Boolean);
}

function taskKey(value) {
  if (typeof value !== 'string' || !/^[A-Z][A-Z0-9_-]{0,15}$/.test(value.trim().toUpperCase())) {
    throw badRequest('invalid_task_key', 'task_key must be a task key, like T3.');
  }
  return value.trim().toUpperCase();
}

function handoff(row) {
  const { team_id, ...out } = row;
  return { ...out, changed_files: parse(row.changed_files, []), open_questions: parse(row.open_questions, []) };
}

function note(row) {
  const { team_id, ...out } = row;
  return { ...out, tags: parse(row.tags, []) };
}

export function createHandoff(hub, member, input) {
  const key = taskKey(input.task_key);
  const task = hub.get('SELECT * FROM tasks WHERE team_id = ? AND key = ?', member.team_id, key);
  if (!task) throw notFound('unknown_task', `There is no task ${key}.`);
  if (task.assignee_id !== member.id && task.created_by !== member.id) {
    throw forbidden('not_your_task', 'Only the task assignee or creator may write a handoff.');
  }
  const summary = text(input.summary, 'summary', 8000, true);
  const changedFiles = strings(input.changed_files, 'changed_files');
  const openQuestions = strings(input.open_questions, 'open_questions');
  const nextStep = text(input.next_step, 'next_step', 8000);
  const result = hub.run(
    'INSERT INTO task_handoffs (team_id, task_key, author_id, summary, changed_files, open_questions, next_step, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
    member.team_id, key, member.id, summary, JSON.stringify(changedFiles), JSON.stringify(openQuestions), nextStep, hub.now(),
  );
  const id = Number(result.lastInsertRowid);
  const row = handoff(hub.get('SELECT * FROM task_handoffs WHERE team_id = ? AND id = ?', member.team_id, id));
  hub.emit(member.team_id, member.id, 'handoff_created', { ...row, key });
  return { handoff: row };
}

export function listHandoffs(hub, member, key) {
  // No task join: replacing a plan must not erase a task's handoff history.
  const rows = key == null
    ? hub.all('SELECT * FROM task_handoffs WHERE team_id = ? ORDER BY created_at DESC, id DESC', member.team_id)
    : hub.all('SELECT * FROM task_handoffs WHERE team_id = ? AND task_key = ? ORDER BY created_at DESC, id DESC', member.team_id, taskKey(key));
  return { handoffs: rows.map(handoff) };
}

export function createNote(hub, member, input) {
  const title = text(input.title, 'title', 200, true);
  const body = text(input.body, 'body', 8000, true);
  const tags = [...new Set(strings(input.tags, 'tags', 10))];
  const result = hub.run(
    'INSERT INTO team_notes (team_id, author_id, title, body, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)',
    member.team_id, member.id, title, body, JSON.stringify(tags), hub.now(),
  );
  const id = Number(result.lastInsertRowid);
  const row = note(hub.get('SELECT * FROM team_notes WHERE team_id = ? AND id = ?', member.team_id, id));
  hub.emit(member.team_id, member.id, 'note_created', row);
  return { note: row };
}

export function listNotes(hub, member, { q, tag }) {
  const query = String(q ?? '').trim().toLowerCase();
  const selectedTag = String(tag ?? '').trim();
  const notes = hub.all('SELECT * FROM team_notes WHERE team_id = ? ORDER BY created_at DESC, id DESC', member.team_id)
    .map(note)
    .filter((row) => (!selectedTag || row.tags.includes(selectedTag)) &&
      (!query || [row.title, row.body, ...row.tags].some((value) => value.toLowerCase().includes(query))));
  return { notes };
}

export function deleteNote(hub, member, id) {
  if (!/^[1-9]\d*$/.test(id) || !Number.isSafeInteger(Number(id))) throw notFound('unknown_note', 'This note does not exist.');
  const row = hub.get('SELECT * FROM team_notes WHERE team_id = ? AND id = ?', member.team_id, Number(id));
  if (!row) throw notFound('unknown_note', 'This note does not exist.');
  if (row.author_id !== member.id) throw forbidden('not_your_note', 'Only the note author may delete it.');
  // Only this explicit, authenticated DELETE removes a note. There is no pruning or tombstone.
  hub.run('DELETE FROM team_notes WHERE team_id = ? AND id = ?', member.team_id, row.id);
  hub.emit(member.team_id, member.id, 'note_deleted', { id: row.id, title: row.title });
  return { ok: true };
}
