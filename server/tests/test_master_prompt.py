import hashlib

import pytest


def edit(api, section="Goal", text="Build together", role="a", **extra):
    result = api.post("/v1/master/sections", {"section": section, "text": text, **extra}, role)
    assert result.status in (200, 201), result.body
    return result.json()


def test_master_sections_are_shared_signed_and_versioned(api):
    initial = api.get("/v1/master").json()
    assert initial["state"]["status"] == "draft"
    assert initial["state"]["locked_version"] == 0
    assert initial["sections"] == initial["edits"] == []
    edit(api, author="agent-b", revision=999)
    edit(api, "Interface", "Publish the contract", "b")
    edit(api, text="Updated scope", role="b")
    master = api.get("/v1/master", "b").json()
    assert master["state"]["lead"] == "agent-a"
    assert master["state"]["revision"] == 3
    assert len(master["sections"]) == 2
    history = master["edits"]
    assert [row["revision"] for row in history] == [1, 2, 3]
    assert [row["author"] for row in history] == ["agent-a", "agent-b", "agent-b"]
    for row in history:
        assert row["sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()
        assert row["created_at"] > 0
    sections = {row["section"]: row for row in master["sections"]}
    assert sections["Goal"]["text"] == "Updated scope"
    assert sections["Goal"]["revision"] == 3


def test_master_approval_locks_v1_and_amendment_locks_v2(api):
    edit(api)
    assert api.post("/v1/master/submit", {}).status == 200
    voting = api.get("/v1/master").json()
    assert voting["state"]["status"] == "voting"
    assert voting["waiting_on"] == ["agent-b"]
    assert any(vote["agent"] == "agent-a" and vote["decision"] == "approve" for vote in voting["votes"])
    assert api.post("/v1/master/sections", {"section": "Goal", "text": "cannot change"}, "b").status == 409
    assert api.post("/v1/master/submit", {}, "b").status == 409
    assert api.post("/v1/master/vote", {"decision": "approve"}, "b").status == 200
    locked = api.get("/v1/master").json()
    assert locked["state"]["status"] == "locked"
    assert locked["state"]["locked_version"] == 1
    assert locked["waiting_on"] == []
    assert api.post("/v1/master/vote", {"decision": "reject"}).status == 409
    edit(api, text="An amendment", role="b")
    amended = api.get("/v1/master").json()
    assert amended["state"]["status"] == "draft"
    assert amended["state"]["locked_version"] == 1
    assert api.post("/v1/master/submit", {}, "b").status == 200
    assert api.get("/v1/master").json()["waiting_on"] == ["agent-a"]
    assert api.post("/v1/master/vote", {"decision": "approve"}).status == 200
    final = api.get("/v1/master").json()
    assert final["state"]["locked_version"] == 2
    assert final["state"]["round"] == locked["state"]["round"] + 1
    assert final["state"]["status"] == "locked"


def test_master_rejection_reopens_draft_and_notifies_other_member(api):
    assert api.post("/v1/master/submit", {}).status == 409
    edit(api)
    assert api.post("/v1/master/submit", {}).status == 200
    assert api.post("/v1/master/vote", {"decision": "reject", "note": "Need interfaces"}, "b").status == 200
    master = api.get("/v1/master").json()
    assert master["state"]["status"] == "draft"
    assert master["state"]["locked_version"] == 0
    events = api.get("/v1/events?after=0", "a").json()["events"]
    assert any(row["type"].startswith("master_") and row["actor"] == "agent-b" for row in events)
    news = api.get("/v1/news?peek=true", "b").json()["news"]
    assert any(row["kind"].startswith("master_") for row in news)


def test_master_new_text_is_cleaned_and_trimmed(api):
    edit(api, "\ufeff G\x00o\x7fal\x85 \ufeff", "\ufeff A\x01B\x9f\n\tC \ufeff")
    section = api.get("/v1/master").json()["sections"][0]
    assert section["section"] == "Goal"
    assert section["text"] == "AB\n\tC"


@pytest.mark.parametrize("payload", [
    {"section": "\x00\x7f", "text": "text"},
    {"section": "Goal", "text": "\x01\x85"},
    {"section": "x" * 81, "text": "text"},
    {"section": "😀" * 41, "text": "text"},
    {"section": "Goal", "text": "x" * 8001},
    {"section": "Goal", "text": "😀" * 4001},
    {"section": False, "text": "text"},
])
def test_master_text_caps_and_types(api, payload):
    assert api.post("/v1/master/sections", payload).status == 400


@pytest.mark.parametrize("payload", [{"decision": "yes"}, {"decision": "approve", "note": "x" * 8001}])
def test_master_vote_validation(api, payload):
    edit(api)
    api.post("/v1/master/submit", {})
    assert api.post("/v1/master/vote", payload, "b").status == 400


def test_master_history_is_bounded_but_sections_keep_latest(api):
    for revision in range(55):
        edit(api, text=f"revision {revision}")
    master = api.get("/v1/master").json()
    assert len(master["edits"]) == 50
    assert [row["revision"] for row in master["edits"]] == list(range(6, 56))
    assert master["sections"][0]["text"] == "revision 54"
