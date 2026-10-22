"""End-to-end check against a running AgentMesh API. Needs both tokens (run it once, then delete them from your shell).

AGENTMESH_URL=https://agents.example.com A_TOKEN=... B_TOKEN=... python3 scripts/e2e_test.py
Optional: AGENTMESH_CF_ACCESS_CLIENT_ID / AGENTMESH_CF_ACCESS_CLIENT_SECRET if Cloudflare Access is enabled.
"""
import os
import threading
import httpx

URL = os.environ["AGENTMESH_URL"].rstrip("/")
extra = {}
if os.environ.get("AGENTMESH_CF_ACCESS_CLIENT_ID"):
    extra = {"CF-Access-Client-Id": os.environ["AGENTMESH_CF_ACCESS_CLIENT_ID"],
             "CF-Access-Client-Secret": os.environ["AGENTMESH_CF_ACCESS_CLIENT_SECRET"]}
A = httpx.Client(base_url=URL, timeout=40, headers={"Authorization": f"Bearer {os.environ['A_TOKEN']}", **extra})
B = httpx.Client(base_url=URL, timeout=40, headers={"Authorization": f"Bearer {os.environ['B_TOKEN']}", **extra})
anon = httpx.Client(base_url=URL, timeout=10, headers=extra)

def ok(r, code=200):
    assert r.status_code == code, f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text}"
    return r.json()

assert anon.get("/health").status_code == 200
assert anon.get("/v1/whoami").status_code == 401
assert anon.get("/v1/whoami", headers={"Authorization": "Bearer wrong", "x-agentmesh-agent": "agent-a"}).status_code == 401
assert ok(A.get("/v1/whoami"))["agent"] == "agent-a"
assert ok(B.get("/v1/whoami"))["agent"] == "agent-b"

# B long-polls for events while A assigns a task.
cursor = ok(B.get("/v1/events"))["last_event_id"]
got = {}
t = threading.Thread(target=lambda: got.update(ok(B.get(f"/v1/events?after={cursor}&wait=20"))))
t.start()
task = ok(A.post("/v1/tasks", json={"title": "e2e", "description": "test", "assignee": "agent-b", "acceptance_criteria": ["works"]}), 201)
t.join()
assert got["events"][0]["type"] == "task_assigned" and got["events"][0]["ref"] == task["id"], got

tid = task["id"]
assert any(x["id"] == tid for x in ok(B.get("/v1/inbox"))["assigned_tasks"])
ok(B.post(f"/v1/tasks/{tid}/complete", json={"summary": "early"}), 409)  # must claim first
ok(A.post(f"/v1/tasks/{tid}/claim", json={}), 403)                     # only assignee
ok(B.post(f"/v1/tasks/{tid}/claim", json={}))
ok(B.post(f"/v1/tasks/{tid}/complete", json={"summary": "x", "status": "bogus"}), 400)
ok(B.post(f"/v1/tasks/{tid}/complete", json={"summary": "did it"}))
ok(B.post(f"/v1/tasks/{tid}/review", json={"decision": "accept"}), 403)  # only creator
ok(A.post(f"/v1/tasks/{tid}/review", json={"decision": "reopen", "note": "add tests"}))
assert any(m["task_id"] == tid for m in ok(B.get("/v1/inbox"))["unread_messages"])
ok(B.post(f"/v1/tasks/{tid}/claim", json={}))
ok(B.post(f"/v1/tasks/{tid}/complete", json={"summary": "added tests"}))
assert ok(A.post(f"/v1/tasks/{tid}/review", json={"decision": "accept"}))["status"] == "accepted"

msg = ok(A.post("/v1/messages", json={"to": "agent-b", "text": "hello", "task_id": tid}), 201)
ok(A.post("/v1/messages", json={"to": "agent-b", "text": "x", "task_id": "../patches"}), 400)
assert any(m["id"] == msg["id"] for m in ok(B.get("/v1/messages?unread=true"))["messages"])
for m in ok(B.get("/v1/messages?unread=true"))["messages"]:
    ok(B.post(f"/v1/messages/{m['id']}/ack", json={}))
assert not ok(B.get("/v1/messages?unread=true"))["messages"]

patch = ok(A.post("/v1/patches", json={"title": "diff", "text": "--- a\n+++ b\n"}), 201)
listed = ok(B.get("/v1/patches"))["patches"]
assert "text" not in listed[0] and listed[0]["id"] == patch["id"]
assert ok(B.get(f"/v1/patches/{patch['id']}"))["patch"]["text"].startswith("---")

ok(A.post("/v1/tasks", content=b"[1]", headers={"content-type": "application/json"}), 400)
ok(A.post("/v1/patches", json={"title": "big", "text": "x" * 300_000}), 413)
print("e2e OK")
