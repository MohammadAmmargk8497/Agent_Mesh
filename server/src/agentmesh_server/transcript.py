"""Read-only projection of the hub's sent messages for a single team."""

import json


def _json(value, default):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def hub_messages(database, team):
    """Keep full source text, rather than the truncated huddle event preview."""
    def rows(sql):
        cursor = database.execute(sql, (team,))
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor]

    names = {row["id"]: row["name"] for row in rows("SELECT id,name FROM members WHERE team_id=?")}
    asks = rows("SELECT * FROM asks WHERE team_id=?")
    posts = rows("SELECT * FROM huddle_posts WHERE team_id=?")
    versions = {row["version"]: _json(row["sections"], []) for row in rows(
        "SELECT version,sections FROM prompt_versions WHERE team_id=?"
    )}
    events = rows("SELECT id,member_id,kind,data,created_at FROM events WHERE team_id=? "
                  "AND (kind IN ('ask','reply','huddle_post','update','decision','prompt_edit','prompt_vote') "
                  "OR kind GLOB 'task_*') ORDER BY id")
    messages = []
    replied = set()
    ask_order, post_order = {}, {}

    def add(id, created_at, sender, recipient, kind, text, order=0, **extra):
        if isinstance(text, str) and text.strip():
            messages.append({
                "id": id, "created_at": created_at,
                "sender": names.get(sender, sender or "team"),
                "recipient": names.get(recipient, recipient or "team"),
                "kind": kind, "text": text, "_order": order, **extra,
            })

    for event in events:
        data = _json(event["data"], {})
        if not isinstance(data, dict):
            continue
        kind, text, extra = None, None, {}
        recipient = data.get("to_id")
        if event["kind"] == "ask" and data.get("ask_id"):
            ask_order.setdefault(data["ask_id"], event["id"])
        elif event["kind"] == "huddle_post":
            post_order.setdefault((data.get("huddle_id"), data.get("seq")), event["id"])
        elif event["kind"] == "reply" and data.get("ask_id"):
            kind, text = "answer", data.get("answer")
            extra["ask_id"] = data["ask_id"]
            if isinstance(text, str) and text.strip():
                replied.add(data["ask_id"])
        elif event["kind"] in ("update", "decision"):
            kind, text = "huddle post", data.get("text")
        elif event["kind"].startswith("task_"):
            kind, text = "task note", data.get("note")
            if data.get("key"):
                extra["task_id"] = data["key"]
        elif event["kind"] == "prompt_vote":
            kind, text = "master-prompt", data.get("comment")
            extra["prompt_version"] = data.get("version")
        elif event["kind"] == "prompt_edit":
            kind = "master-prompt"
            extra["prompt_version"] = data.get("version")
            sections = versions.get(data.get("version"), [])
            if isinstance(sections, list):
                section = next((section for section in sections if isinstance(section, dict)
                                and section.get("title") == data.get("section")), {})
                text = section.get("body")
        if kind:
            add(f"event:{event['id']}", event["created_at"], event["member_id"],
                recipient, kind, text, order=event["id"], **extra)

    for ask in asks:
        add(f"ask:{ask['id']}", ask["created_at"], ask["from_id"], ask["to_id"],
            "ask", ask["question"], order=ask_order.get(ask["id"], 0), ask_id=ask["id"])
        # Reply events preserve every answer even when a later reply replaces it.
        # Imported/restored asks can have a current answer but no matching event.
        if ask["id"] not in replied:
            add(f"answer:{ask['id']}", ask["answered_at"] if ask["answered_at"] is not None else ask["created_at"],
                ask["to_id"], ask["from_id"], "answer", ask["answer"],
                order=ask_order.get(ask["id"], 0) + 0.5, ask_id=ask["id"])
    for post in posts:
        add(f"huddle:{post['huddle_id']}:{post['seq']}", post["created_at"],
            post["member_id"], None,
            "huddle comment" if post["kind"] == "comment" else "huddle post",
            post["text"], order=post_order.get((post["huddle_id"], post["seq"]), 0),
            huddle_id=post["huddle_id"])
    messages.sort(key=lambda message: (message["created_at"], message["_order"], message["id"]))
    for message in messages:
        del message["_order"]
    return messages[-500:]


def merge_messages(direct, hub):
    """Return the newest 500 entries, in a stable oldest-to-newest order."""
    messages = [{**message, "kind": "direct"} for message in direct] + hub
    return sorted(messages, key=lambda message: message["created_at"])[-500:]
