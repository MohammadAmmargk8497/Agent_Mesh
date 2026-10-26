"""HTTP/1.1 transport for the AgentMesh Worker-compatible API."""

import base64
import html
import http.client
import ipaddress
import json
import logging
import math
import re
import secrets
import socket
import sqlite3
import time
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from . import coordination
from .config import load_config
from .hub import HubSupervisor, HubUnavailable
from .state import AGENTS, MAX_WAIT_S, MAX_WAITERS_PER_AGENT, State, now_ms

MAX_BODY = 256 * 1024
MAX_PATCH = 200 * 1024
JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
TASK_PATH = re.compile(r"/v1/tasks/([0-9a-f-]{36})(?:/(claim|complete|review|cancel))?", re.I)
ACK_PATH = re.compile(r"/v1/messages/([0-9a-f-]{36})/ack", re.I)
PATCH_PATH = re.compile(r"/v1/patches/([0-9a-f-]{36})", re.I)


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _ip(value):
    address = ipaddress.ip_address(value)
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address


def peer_ip(peer, forwarded_for=None):
    """Trust only the last proxy-appended address, and only from loopback."""
    try:
        address = _ip(peer)
    except ValueError:
        return peer
    if address.is_loopback and forwarded_for:
        try:
            return str(_ip(forwarded_for.rsplit(",", 1)[-1].strip()))
        except ValueError:
            pass
    return str(address)


def _loopback_bind(host):
    try:
        return _ip(host).is_loopback
    except ValueError:
        # Hostnames may be rebound later; only the conventional local name is
        # accepted without the explicit remote-bind flag.
        if host.lower() != "localhost":
            return False
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return bool(addresses) and all(_ip(item[4][0]).is_loopback for item in addresses)


def valid_string(value, maximum=4000):
    # JavaScript's String.length counts UTF-16 code units, not code points.
    return (isinstance(value, str) and bool(value.strip(JS_WHITESPACE)) and
            len(value.encode("utf-16-le", errors="surrogatepass")) // 2 <= maximum)


def optional_id(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not UUID.fullmatch(value):
        raise HttpError(400, "task_id must be a UUID")
    return value


def _number(value):
    """The URL parameter subset of JavaScript Number(), including empty zero."""
    value = value.strip(JS_WHITESPACE)
    if not value:
        return 0.0
    try:
        if re.fullmatch(r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+", value):
            return float(int(value, 0))
        if not re.fullmatch(r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|Infinity)", value):
            return math.nan
        return float(value)
    except (ValueError, OverflowError):
        return math.nan


def _task_dict(row, state):
    row["acceptance_criteria"] = json.loads(row["acceptance_criteria"])
    return coordination.task_fields(state, row)


class AgentMeshHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_close(self):
        try:
            super().server_close()
        finally:
            if getattr(self, "hub", None) is not None:
                self.hub.close()
            if getattr(self, "state", None) is not None:
                state, self.state = self.state, None
                state.close()


def create_server(config_dir=None, host="127.0.0.1", port=8787, allow_remote_bind=False,
                  *, hub=True, public_url=None):
    """Create a bound server; call serve_forever(), then server_close()."""
    if not allow_remote_bind and not _loopback_bind(host):
        raise ValueError("non-loopback bind requires --allow-remote-bind")
    config = load_config(config_dir)
    state = State(config_dir, config)
    # Legacy configs may retain obsolete token roles; only agents authenticate.
    state.token_hashes = [(agent, digest) for agent, digest in state.token_hashes if agent in AGENTS]
    server_type = AgentMeshHTTPServer
    if ":" in host:
        class IPv6Server(AgentMeshHTTPServer):
            address_family = socket.AF_INET6
        server_type = IPv6Server
    try:
        server = server_type((host, port), Handler)
    except BaseException:
        state.close()
        raise
    server.state = state
    server.hub = HubSupervisor(config_dir, config, *server.server_address[:2],
                               enabled=hub, public_url=public_url)
    return server


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgentMesh"
    sys_version = ""

    def setup(self):
        self.request.settimeout(35)
        super().setup()

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):
        # Avoid logging credentials that a caller might put in a URL.
        pass

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._json({"error": message or self.responses.get(code, ("error",))[0]}, code)

    def handle_expect_100(self):
        try:
            self._framing()
        except HttpError as exc:
            self.close_connection = True
            self._json({"error": str(exc)}, exc.status)
            return False
        return super().handle_expect_100()

    def _framing(self):
        lengths = self.headers.get_all("Content-Length", [])
        transfers = self.headers.get_all("Transfer-Encoding", [])
        if len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers):
            raise HttpError(400, "invalid request body framing")
        if transfers:
            if transfers[0].strip().lower() != "chunked":
                raise HttpError(400, "unsupported transfer encoding")
            return None
        if not lengths:
            return 0
        if not re.fullmatch(r"[0-9]+", lengths[0].strip()):
            raise HttpError(400, "invalid content-length")
        try:
            length = int(lengths[0])
        except ValueError:
            raise HttpError(413, "request body too large") from None
        if length > MAX_BODY:
            raise HttpError(413, "request body too large")
        return length

    def _exact(self, length):
        content = self.rfile.read(length)
        if len(content) != length:
            raise HttpError(400, "incomplete request body")
        return content

    def _read_body(self):
        length = self._framing()
        if length is not None:
            body = self._exact(length)
        else:
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(65537)
                if len(line) > 65536 or not line.endswith(b"\r\n"):
                    raise HttpError(400, "invalid chunk framing")
                size_text = line[:-2].split(b";", 1)[0]
                if not re.fullmatch(rb"[0-9a-fA-F]+", size_text):
                    raise HttpError(400, "invalid chunk framing")
                size = int(size_text, 16)
                if total + size > MAX_BODY:
                    raise HttpError(413, "request body too large")
                if size == 0:
                    for _ in range(100):
                        trailer = self.rfile.readline(65537)
                        if trailer == b"\r\n":
                            break
                        if len(trailer) > 65536 or not trailer.endswith(b"\r\n"):
                            raise HttpError(400, "invalid chunk trailer")
                    else:
                        raise HttpError(400, "too many chunk trailers")
                    break
                chunks.append(self._exact(size))
                total += size
                if self._exact(2) != b"\r\n":
                    raise HttpError(400, "invalid chunk framing")
            body = b"".join(chunks)
        if len(body) > MAX_BODY:
            raise HttpError(413, "request body too large")
        return body

    def _body_json(self):
        try:
            def invalid_constant(value):
                raise ValueError(value)
            body = json.loads(self.body.decode("utf-8-sig", errors="replace"), parse_constant=invalid_constant)
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "invalid JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "body must be a JSON object")
        return body

    def _send(self, content, status, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(self, data, status=200):
        content = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self._send(content, status, "application/json; charset=utf-8")

    def _dashboard(self, name="dashboard.html"):
        nonce = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        html = files("agentmesh_server").joinpath(name).read_bytes()
        html = html.replace(b"__AGENTMESH_NONCE__", nonce.encode("ascii"))
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; "
               f"style-src 'nonce-{nonce}' https://fonts.googleapis.com; "
               "font-src https://fonts.gstatic.com; connect-src 'self'; "
               "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self._send(html, 200, "text/html; charset=utf-8", {"Content-Security-Policy": csp})

    def _hub_page(self, path):
        hub = self.server.hub
        hub.available()
        if re.fullmatch(r"/t/[a-z0-9]+", path):
            name = "dashboard.html"
        elif re.fullmatch(r"/join/[a-z0-9]+\.[A-Za-z0-9_-]+", path):
            name = "hub_join.html"
        else:
            raise HttpError(404, "not found")
        nonce = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        page = files("agentmesh_server").joinpath(name).read_text(encoding="utf-8")
        for key, value in (("HUB_URL", hub.public_url), ("INVITE_URL", hub.public_url + path)):
            page = page.replace("{{" + key + "}}", html.escape(value, quote=True))
        page = page.replace("__AGENTMESH_NONCE__", nonce)
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; "
               f"style-src 'nonce-{nonce}' https://fonts.googleapis.com; "
               "font-src https://fonts.gstatic.com; connect-src 'self'; "
               "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self._send(page.encode(), 200, "text/html; charset=utf-8", {
            "Content-Security-Policy": csp, "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
        })

    def _hub_proxy(self):
        endpoint = self.server.hub.endpoint()
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=35)
        hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
               "x-forwarded-for"}
        hop.update(value.strip().lower() for value in self.headers.get("Connection", "").split(","))
        headers = {name: value for name, value in self.headers.items() if name.lower() not in hop}
        headers["X-Forwarded-For"] = self.client_address[0]
        try:
            connection.request(self.command, self.path, self.body, headers)
            response = connection.getresponse()
            streaming = response.getheader("Content-Type", "").split(";", 1)[0] == "text/event-stream"
            self.send_response(response.status)
            response_hop = {"connection", "keep-alive", "transfer-encoding", "trailer", "upgrade"}
            for name, value in response.getheaders():
                if name.lower() not in response_hop:
                    self.send_header(name, value)
            # http.client decodes upstream chunks. Close-delimiting also keeps SSE
            # unbuffered, rather than inventing a Content-Length or accumulating it.
            if streaming or response.getheader("Content-Length") is None:
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (OSError, http.client.HTTPException):
            self.close_connection = True
            # Once headers are sent (especially an SSE response), do not emit a
            # second HTTP response into the stream.
            if "response" not in locals():
                raise HubUnavailable("hub unavailable") from None
        finally:
            connection.close()

    def _legacy_adapter(self, url):
        """Legacy snapshots for authenticated default-team members."""
        path = url.path[len("/api/legacy"):]
        if self.command != "GET":
            raise HttpError(405, "method not allowed")
        if path not in ("/transcript", "/patches") and not PATCH_PATH.fullmatch("/v1" + path):
            raise HttpError(404, "not found")
        hub = self.server.hub
        endpoint = hub.endpoint()
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=35)
        try:
            connection.request("GET", "/api/state", headers={
                "Authorization": self.headers.get("Authorization", ""),
                "X-Forwarded-For": self.client_address[0],
            })
            response = connection.getresponse()
            content = response.read()
            if response.status != 200:
                self._send(content, response.status, "application/json; charset=utf-8")
                return
            member_state = json.loads(content)
        except (OSError, http.client.HTTPException, ValueError):
            raise HubUnavailable("hub unavailable") from None
        finally:
            connection.close()
        query = parse_qs(url.query, keep_blank_values=True)
        team = query.get("team", [None])[0]
        if not team or team != member_state.get("team", {}).get("id"):
            raise HttpError(403, "team does not match member key")
        # The seed ledger is immutable metadata, unlike team names. Read it without
        # authenticating as an agent (which would falsely refresh that agent's presence).
        try:
            with closing(sqlite3.connect(hub._database_path().as_uri() + "?mode=ro", uri=True)) as database:
                seeded = database.execute("SELECT team_id FROM agentmesh_seed LIMIT 1").fetchone()
        except (OSError, ValueError, sqlite3.Error):
            raise HubUnavailable("hub unavailable") from None
        if not seeded or team != seeded[0]:
            raise HttpError(403, "legacy panels are available only for the default team")
        state = self.server.state
        if state is None or state.stop.is_set():
            raise HttpError(503, "server shutting down")
        with state.condition:
            if path == "/transcript":
                data, status = self._snapshot(state, query)
            else:
                data, status = self._route(state, "agent-a", "/v1" + path, query)
        self._json(data, status)

    def _handle(self):
        try:
            self.body = self._read_body()
            url = urlsplit(self.path)
            if self.command in ("GET", "HEAD") and url.path in ("/index.html", "/viewer"):
                self._send(b"", 302, "text/plain; charset=utf-8", {"Location": "/"})
                return
            if self.command in ("GET", "HEAD") and url.path == "/":
                self._dashboard()
                return
            if url.path.startswith("/api/legacy/"):
                self._legacy_adapter(url)
                return
            if url.path == "/api" or url.path.startswith("/api/"):
                self._hub_proxy()
                return
            if url.path in ("/t", "/join") or url.path.startswith(("/t/", "/join/")):
                if self.command not in ("GET", "HEAD"):
                    raise HttpError(404, "not found")
                self._hub_page(url.path)
                return
            if url.path == "/robots.txt" and self.command in ("GET", "HEAD"):
                self._send(b"User-agent: *\nDisallow: /t/\nDisallow: /join/\n", 200,
                           "text/plain; charset=utf-8")
                return
            state = self.server.state
            if state is None or state.stop.is_set():
                raise HttpError(503, "server shutting down")
            header = self.headers.get("Authorization", "")
            token = header[7:].strip() if header.startswith("Bearer ") else ""
            agent = state.identify(token) if token else None
            if agent not in AGENTS:
                agent = None
            if url.path == "/health" and self.command == "GET":
                self._json({"ok": True, "service": "agentmesh"})
                return
            key = agent
            if key is None:
                peer = self.client_address[0]
                forwarded_for = ",".join(self.headers.get_all("X-Forwarded-For", [])) if _ip(peer).is_loopback else None
                key = "ip:" + peer_ip(peer, forwarded_for)
            if not state.limiter.allow(key):
                raise HttpError(429, "rate limited")
            if agent is None:
                raise HttpError(401, "unauthorized")
            path = url.path.rstrip("/") or "/"
            query = parse_qs(url.query, keep_blank_values=True)
            with state.condition:
                if state.stop.is_set():
                    raise HttpError(503, "server shutting down")
                transaction = self.command == "POST"
                if transaction:
                    state.db.execute("BEGIN IMMEDIATE")
                try:
                    data, status = self._route(state, agent, path, query)
                except BaseException:
                    if transaction:
                        state.db.rollback()
                    raise
                else:
                    if transaction:
                        state.db.commit()
            self._json(data, status)
        except HubUnavailable:
            self._json({"error": "hub unavailable"}, 503)
        except (HttpError, coordination.CoordinationError) as exc:
            # Invalid framing/body may leave unread bytes: never reuse it as
            # the next request on this connection.
            if exc.status in (400, 408, 413):
                self.close_connection = True
            self._json({"error": str(exc), **getattr(exc, "details", {})}, exc.status)
        except (TimeoutError, socket.timeout):
            self.close_connection = True
            self._json({"error": "request timed out"}, 408)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            logging.exception("AgentMesh request failed")
            self.close_connection = True
            self._json({"error": "internal error"}, 500)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle

    def _snapshot(self, state, query, agent=None):
        """Build a member-authorized transcript or an agent-scoped dashboard."""
        now = now_ms()
        rows = state.rows
        scoped = agent is not None
        raw_after = query.get("after", [None])[0]
        if raw_after is not None and not re.fullmatch(r"-?\d+", raw_after, re.ASCII):
            raise HttpError(400, "after must be an integer")
        after_number = _number(raw_after) if raw_after is not None else 0
        if not math.isfinite(after_number) or abs(after_number) > 2**53 - 1:
            raise HttpError(400, "after must be an integer")
        after = int(after_number)
        events = rows("SELECT * FROM events WHERE id>?" + (" AND recipient=?" if scoped else "") + " ORDER BY id ASC LIMIT 200", after, *((agent,) if scoped else ()))
        tasks = [_task_dict(row, state) for row in rows("SELECT * FROM tasks" + (" WHERE created_by=? OR assignee=? OR (status='open' AND id IN (SELECT task_id FROM dashboard_tasks WHERE state='open'))" if scoped else "") + " ORDER BY created_at DESC LIMIT 200", *((agent, agent) if scoped else ()))[::-1]]
        if scoped:
            for task in tasks:
                task["dashboard_status"] = coordination.dashboard_status(state, task)
        return {
            "messages": rows("SELECT * FROM messages" + (" WHERE sender=? OR recipient=?" if scoped else "") + " ORDER BY created_at DESC LIMIT 500", *((agent, agent) if scoped else ()))[::-1],
            "tasks": tasks,
            "patches": rows("SELECT id,sender,title,task_id,created_at,length(text) AS size FROM patches ORDER BY created_at DESC LIMIT 50")[::-1],
            "audit": rows("SELECT * FROM audit" + (" WHERE actor=?" if scoped else "") + " ORDER BY id DESC LIMIT 200", *((agent,) if scoped else ()))[::-1],
            "events": events,
            "last_event_id": events[-1]["id"] if events else after,
            **coordination.snapshot(state, now, agent=agent),
            **({"me": agent} if scoped else {}),
        }, 200

    def _route(self, state, agent, path, query):
        method, now = self.command, now_ms()

        def param(name, default=None):
            return query.get(name, [default])[0]

        rows = state.rows
        db = state.db
        if path == "/v1/dashboard/state" and method == "GET":
            return self._snapshot(state, query, agent)
        if path == "/v1/transcript":
            raise HttpError(403, "member key required")
        coordinated = coordination.route(self, state, agent, path, query, now)
        if coordinated is not None:
            return coordinated
        if path == "/v1/whoami" and method == "GET":
            return {"agent": agent}, 200
        if path == "/v1/inbox" and method == "GET":
            return {
                "agent": agent,
                "unread_messages": rows("SELECT * FROM messages WHERE recipient=? AND acked_at IS NULL ORDER BY created_at ASC LIMIT 100", agent),
                "assigned_tasks": [_task_dict(row, state) for row in rows("SELECT * FROM tasks WHERE assignee=? AND status IN ('open','in_progress') ORDER BY created_at ASC LIMIT 100", agent)],
                "tasks_to_review": [_task_dict(row, state) for row in rows("SELECT * FROM tasks WHERE created_by=? AND assignee<>? AND status IN ('done','blocked') ORDER BY updated_at ASC LIMIT 100", agent, agent)],
                "last_event_id": state.last_event_id(agent),
            }, 200
        if path == "/v1/events" and method == "GET":
            after_number = _number(param("after")) if "after" in query else state.last_event_id(agent)
            wait = _number(param("wait", "0"))
            if not math.isfinite(after_number) or not float(after_number).is_integer() or math.isnan(wait):
                raise HttpError(400, "after must be an integer and wait a number")
            after = int(after_number)
            sql_after = min(max(after, -(2**63)), 2**63 - 1)
            wait = min(max(wait, 0), MAX_WAIT_S)
            def read():
                return rows("SELECT * FROM events WHERE recipient=? AND id>? ORDER BY id ASC LIMIT 100", agent, sql_after)

            events = read()
            if not events and wait > 0:
                if state.waiters[agent] >= MAX_WAITERS_PER_AGENT:
                    raise HttpError(429, "too many open connections")
                state.waiters[agent] += 1
                try:
                    deadline = time.monotonic() + wait
                    while not events and not state.stop.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        state.condition.wait(remaining)
                        if state.stop.is_set():
                            break
                        events = read()
                    if state.stop.is_set():
                        raise HttpError(503, "server shutting down")
                finally:
                    state.waiters[agent] -= 1
            return {"events": events, "last_event_id": events[-1]["id"] if events else after}, 200
        if path == "/v1/tasks" and method == "GET":
            status = param("status")
            tasks = rows("SELECT * FROM tasks WHERE (assignee=? OR created_by=?) " +
                         ("AND status=? " if status else "") + "ORDER BY created_at DESC LIMIT 100",
                         agent, agent, *((status,) if status else ()))
            return {"tasks": [_task_dict(row, state) for row in tasks]}, 200
        if path == "/v1/tasks" and method == "POST":
            body = self._body_json()
            if not valid_string(body.get("title"), 200) or not valid_string(body.get("description"), 8000):
                raise HttpError(400, "title and description are required")
            if body.get("assignee") not in AGENTS:
                raise HttpError(400, "assignee must be agent-a or agent-b")
            criteria = body.get("acceptance_criteria")
            criteria = [] if criteria is None else criteria
            if not isinstance(criteria, list) or len(criteria) > 20 or any(not valid_string(value, 500) for value in criteria):
                raise HttpError(400, "acceptance_criteria must contain at most 20 strings")
            options = coordination.task_options(state, agent, body)
            task_id = str(uuid.uuid4())
            db.execute("INSERT INTO tasks (id,title,description,assignee,created_by,status,acceptance_criteria,result,created_at,updated_at) VALUES (?,?,?,?,?,'open',?,NULL,?,?)",
                       (task_id, body["title"].strip(JS_WHITESPACE), body["description"].strip(JS_WHITESPACE), body["assignee"], agent, json.dumps(criteria), now, now))
            coordination.save_task_options(state, task_id, options)
            coordination.news(state, agent, "task_created", task_id, "Task created", now)
            state.emit(body["assignee"], agent, "task_assigned", task_id)
            return {"id": task_id, "status": "open", "created_by": agent}, 201
        task_match = TASK_PATH.fullmatch(path)
        if task_match:
            task_id, action = task_match.groups()
            matches = rows("SELECT * FROM tasks WHERE id=?", task_id)
            if not matches or (matches[0]["assignee"] != agent and matches[0]["created_by"] != agent):
                raise HttpError(404, "task not found")
            task = _task_dict(matches[0], state)
            if method == "GET" and action is None:
                return {"task": task}, 200
            if method != "POST":
                raise HttpError(405, "method not allowed")
            if action == "claim":
                if task["assignee"] != agent:
                    raise HttpError(403, "only assignee may claim")
                if task["status"] != "open":
                    raise HttpError(409, "task is not open")
                if task["waiting_on"]:
                    raise coordination.CoordinationError(409, "waiting on dependencies", waiting_on=task["waiting_on"])
                db.execute("UPDATE tasks SET status='in_progress',updated_at=? WHERE id=?", (now, task_id))
                state.emit(task["created_by"], agent, "task_claimed", task_id)
                coordination.news(state, agent, "task_claimed", task_id, "Task claimed", now)
                return {"id": task_id, "status": "in_progress"}, 200
            if action == "complete":
                if task["assignee"] != agent:
                    raise HttpError(403, "only assignee may complete")
                if task["status"] != "in_progress":
                    raise HttpError(409, "claim the task before completing it")
                body = self._body_json()
                if not valid_string(body.get("summary"), 8000):
                    raise HttpError(400, "summary is required")
                if "status" in body and body["status"] not in ("done", "blocked"):
                    raise HttpError(400, "status must be done or blocked")
                status = body.get("status", "done")
                db.execute("UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?", (status, body["summary"], now, task_id))
                state.emit(task["created_by"], agent, "task_" + status, task_id)
                coordination.news(state, agent, "task_" + status, task_id, "Task " + status, now)
                if status == "done":
                    coordination.unblocked(state, agent, task_id, now)
                return {"id": task_id, "status": status}, 200
            if action == "review":
                if task["created_by"] != agent:
                    raise HttpError(403, "only the creator may review")
                if task["status"] not in ("done", "blocked"):
                    raise HttpError(409, "task is not awaiting review")
                body = self._body_json()
                if body.get("decision") not in ("accept", "reopen"):
                    raise HttpError(400, "decision must be accept or reopen")
                if "note" in body and not valid_string(body["note"], 8000):
                    raise HttpError(400, "note must be a non-empty string")
                status = "accepted" if body["decision"] == "accept" else "open"
                db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, now, task_id))
                if body.get("note") and task["assignee"] != agent:
                    db.execute("INSERT INTO messages (id,sender,recipient,text,task_id,created_at,acked_at) VALUES (?,?,?,?,?,?,NULL)",
                               (str(uuid.uuid4()), agent, task["assignee"], f"[review: {body['decision']}] {body['note']}", task_id, now))
                state.emit(task["assignee"], agent, "task_accepted" if status == "accepted" else "task_reopened", task_id)
                coordination.news(state, agent, "task_accepted" if status == "accepted" else "task_reopened", task_id, "Task " + ("accepted" if status == "accepted" else "reopened"), now)
                if status == "accepted" and task["status"] != "done":
                    coordination.unblocked(state, agent, task_id, now)
                return {"id": task_id, "status": status}, 200
            if action == "cancel":
                if task["created_by"] != agent:
                    raise HttpError(403, "only the creator may cancel")
                if task["status"] in ("accepted", "cancelled"):
                    raise HttpError(409, "task is already closed")
                db.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (now, task_id))
                state.emit(task["assignee"], agent, "task_cancelled", task_id)
                coordination.news(state, agent, "task_cancelled", task_id, "Task cancelled", now)
                return {"id": task_id, "status": "cancelled"}, 200
        if path == "/v1/messages" and method == "GET":
            unread = param("unread") == "true"
            messages = rows("SELECT * FROM messages WHERE recipient=? " +
                            ("AND acked_at IS NULL " if unread else "") + "ORDER BY created_at DESC LIMIT 100", agent)
            return {"messages": messages[::-1]}, 200
        if path == "/v1/messages" and method == "POST":
            body = self._body_json()
            if body.get("to") not in AGENTS:
                raise HttpError(400, "to must be agent-a or agent-b")
            if body["to"] == agent:
                raise HttpError(400, "cannot message yourself")
            if not valid_string(body.get("text"), 8000):
                raise HttpError(400, "text is required")
            task_id = optional_id(body.get("task_id"))
            message_id = str(uuid.uuid4())
            db.execute("INSERT INTO messages (id,sender,recipient,text,task_id,created_at,acked_at) VALUES (?,?,?,?,?,?,NULL)",
                       (message_id, agent, body["to"], body["text"], task_id, now))
            db.execute("INSERT INTO audit (actor,action,ref,created_at) VALUES (?,?,?,?)", (agent, "message.send", message_id, now))
            state.emit(body["to"], agent, "message", message_id)
            return {"id": message_id, "created_at": now}, 201
        ack_match = ACK_PATH.fullmatch(path)
        if ack_match and method == "POST":
            changed = db.execute("UPDATE messages SET acked_at=? WHERE id=? AND recipient=? AND acked_at IS NULL",
                                 (now, ack_match[1], agent)).rowcount
            return {"ok": True, "changed": changed}, 200
        if path == "/v1/patches" and method == "GET":
            return {"patches": rows("SELECT id,sender,title,task_id,created_at,length(text) AS size FROM patches ORDER BY created_at DESC LIMIT 50")}, 200
        patch_match = PATCH_PATH.fullmatch(path)
        if patch_match and method == "GET":
            patches = rows("SELECT * FROM patches WHERE id=?", patch_match[1])
            if not patches:
                raise HttpError(404, "patch not found")
            return {"patch": patches[0]}, 200
        if path == "/v1/patches" and method == "POST":
            body = self._body_json()
            if not valid_string(body.get("title"), 200) or not valid_string(body.get("text"), MAX_PATCH):
                raise HttpError(400, "title and text required; text max is 200 KiB")
            task_id = optional_id(body.get("task_id"))
            patch_id = str(uuid.uuid4())
            db.execute("INSERT INTO patches (id,sender,title,text,task_id,created_at) VALUES (?,?,?,?,?,?)",
                       (patch_id, agent, body["title"], body["text"], task_id, now))
            for other in AGENTS:
                state.emit(other, agent, "patch", patch_id)
            return {"id": patch_id, "created_at": now}, 201
        # WebSockets remain Worker-only; clients fall back to long polling.
        raise HttpError(404, "not found")
