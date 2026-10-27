#!/usr/bin/env python3
"""Pull a complete, paged owner export without pruning any previous backup.

The private configuration file is data, not shell code. Supported keys are
ADMIN_EXPORT_TOKEN, AGENTMESH_URL, CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET;
environment variables with those names override the file. No token CLI argument
is accepted, so credentials do not appear in process listings.
"""

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CONFIG_KEYS = ("ADMIN_EXPORT_TOKEN", "AGENTMESH_URL", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
ROWID_PATTERN = re.compile(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z")


class BackupError(Exception):
    """An intentionally credential-free failure message."""


def load_settings(token_file, environ=None):
    environ = os.environ if environ is None else environ
    settings = {}
    try:
        fd = os.open(token_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        pass  # Explicit environment configuration needs no file.
    except OSError:
        raise BackupError("cannot open private token file") from None
    else:
        with os.fdopen(fd, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise BackupError("token file must be a regular file with permissions 0600 or stricter")
            try:
                for line in stream:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[7:].lstrip()
                    key, separator, value = line.partition("=")
                    if not separator:
                        raise BackupError("invalid private token file format")
                    key, value = key.strip(), value.strip()
                    if key not in CONFIG_KEYS:
                        continue
                    if value[:1] in ("'", '"'):
                        if len(value) < 2 or value[-1] != value[0]:
                            raise BackupError("invalid private token file quoting")
                        value = value[1:-1]
                    settings[key] = value
            except UnicodeError:
                raise BackupError("private token file is not UTF-8") from None
    settings.update({key: environ[key] for key in CONFIG_KEYS if key in environ})
    token = settings.get("ADMIN_EXPORT_TOKEN", "")
    if len(token) < 32 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise BackupError("ADMIN_EXPORT_TOKEN must contain at least 32 printable non-space ASCII characters")
    for key in ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"):
        if any(ord(char) < 32 or ord(char) > 126 for char in settings.get(key, "")):
            raise BackupError("invalid Cloudflare Access credentials")
    if bool(settings.get("CF_ACCESS_CLIENT_ID")) != bool(settings.get("CF_ACCESS_CLIENT_SECRET")):
        raise BackupError("both Cloudflare Access credentials must be configured together")
    return settings


def validate_url(value, allow_local_http=False):
    try:
        if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError
        parts = urlsplit(value)
        _ = parts.port  # Validate malformed and out-of-range ports.
        loopback = parts.hostname == "localhost"
        if parts.hostname and not loopback:
            try:
                loopback = ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                pass
        secure = parts.scheme == "https"
        local_http = allow_local_http and parts.scheme == "http" and loopback
        if (not (secure or local_http) or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment or parts.path not in ("", "/")):
            raise ValueError
    except (ValueError, TypeError):
        raise BackupError("AGENTMESH_URL must be an HTTPS origin (HTTP requires explicit loopback testing)") from None
    return value.rstrip("/")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward owner or Access credentials to a redirect.


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("export response contains duplicate JSON keys")
        result[key] = value
    return result


def _invalid_constant(value):
    raise BackupError("export response contains a non-finite JSON number")


class ExportClient:
    def __init__(self, base_url, token, access_id="", access_secret="", timeout=60, delay=0.55):
        self.base_url = base_url
        self.timeout = timeout
        self.delay = delay
        self.opener = build_opener(NoRedirects())
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if access_id:
            self.headers.update({"CF-Access-Client-Id": access_id, "CF-Access-Client-Secret": access_secret})
        self._last_request = None

    def get_json(self, path, parameters=None):
        if self._last_request is not None:
            time.sleep(max(0, self.delay - (time.monotonic() - self._last_request)))
        url = self.base_url + path
        if parameters:
            url += "?" + urlencode(parameters)
        request = Request(url, headers=self.headers, method="GET")
        self._last_request = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise BackupError("export request returned a non-200 response")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise BackupError(f"export request failed with HTTP {error.code}; no pages were skipped") from None
        except (URLError, OSError, ValueError, HTTPException):
            raise BackupError("export network request failed; no pages were skipped") from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise BackupError("export response exceeds the safe page size")
        try:
            return json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise BackupError("export response is not valid JSON") from None


def _tables(document):
    if not isinstance(document, dict) or not isinstance(document.get("tables"), list):
        raise BackupError("invalid table inventory")
    tables, seen = [], set()
    for item in document["tables"]:
        if not isinstance(item, dict):
            raise BackupError("invalid table descriptor")
        store, name, count, sql = (item.get(key) for key in ("store", "name", "count", "sql"))
        if (store not in ("room", "hub") or not isinstance(name, str) or not name or "\0" in name
                or type(count) is not int or count < 0 or not isinstance(sql, str) or not sql):
            raise BackupError("invalid table descriptor")
        identity = (store, name)
        if identity in seen:
            raise BackupError("duplicate table descriptor")
        seen.add(identity)
        tables.append({"store": store, "name": name, "count": count, "sql": sql})
    if {table["store"] for table in tables} != {"room", "hub"}:
        raise BackupError("table inventory is empty or omits an expected store")
    return tables


def _rowid(value):
    if not isinstance(value, str) or not ROWID_PATTERN.fullmatch(value) or len(value) > 20:
        raise BackupError("invalid export rowid")
    number = int(value)
    if not -(2 ** 63) <= number < 2 ** 63:
        raise BackupError("export rowid is outside signed SQLite int64 range")
    return number


def _page(document, store, table, after, limit):
    if (not isinstance(document, dict) or document.get("store") != store or document.get("table") != table
            or not isinstance(document.get("rows"), list) or type(document.get("done")) is not bool
            or "next_after" not in document):
        raise BackupError("invalid export page identity or schema")
    rows = document["rows"]
    if len(rows) > limit:
        raise BackupError("export page exceeds the requested row limit")
    previous = _rowid(after) if after is not None else None
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
            raise BackupError("invalid export row")
        current = _rowid(row.get("rowid"))
        if previous is not None and current <= previous:
            raise BackupError("export rowids are duplicated or out of order")
        previous = current
    expected_cursor = rows[-1]["rowid"] if rows else None
    if document["next_after"] != expected_cursor or (not rows and not document["done"]):
        raise BackupError("export cursor did not make valid progress")
    return rows, expected_cursor, document["done"]


def _new_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def backup(client, backup_dir, page_limit=1000):
    if type(page_limit) is not int or not 1 <= page_limit <= 1000:
        raise BackupError("page limit must be between 1 and 1000")
    root = Path(backup_dir).expanduser()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    directory = root / (now.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:12])
    directory.mkdir(mode=0o700, exist_ok=False)
    tables = _tables(client.get_json("/admin/export/tables"))
    entries = []
    for table in tables:
        store, name, expected = table["store"], table["name"], table["count"]
        filename = store + "-" + hashlib.sha256(name.encode("utf-8")).hexdigest() + ".json"
        digest, count, after = hashlib.sha256(), 0, None
        with _new_file(directory / filename) as stream:
            def write(data, stream=stream, digest=digest):
                stream.write(data)
                digest.update(data)

            write(b"[\n")
            while True:
                parameters = {"store": store, "table": name, "limit": page_limit}
                if after is not None:
                    parameters["after"] = after
                rows, cursor, done = _page(
                    client.get_json("/admin/export/page", parameters), store, name, after, page_limit,
                )
                for row in rows:
                    if count:
                        write(b",\n")
                    write(json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8"))
                    count += 1
                if count > expected:
                    raise BackupError("row count mismatch: more rows pulled than the inventory; backup is incomplete")
                if done:
                    break
                after = cursor
            if count != expected:
                raise BackupError("row count mismatch: fewer rows pulled than the inventory; backup is incomplete")
            write(b"\n]\n")
            stream.flush()
            os.fsync(stream.fileno())
        entries.append({"store": store, "table": name, "sql": table["sql"], "file": filename,
                        "expected_count": expected, "count": count, "sha256": digest.hexdigest()})
    final_tables = _tables(client.get_json("/admin/export/tables"))
    def inventory(items):
        return {(item["store"], item["name"]): (item["count"], item["sql"]) for item in items}

    if inventory(tables) != inventory(final_tables):
        raise BackupError("table inventory changed during export; backup is incomplete")
    manifest = {"format_version": 1, "created_at": now.isoformat().replace("+00:00", "Z"),
                "source": client.base_url, "complete": True, "tables": entries}
    # Publish a completed manifest only after every table has passed its count
    # check and its data is flushed. A hard link is exclusive and cannot replace
    # an existing manifest. Keep the pending file too: this tool never unlinks.
    pending = directory / ".manifest.pending"
    with _new_file(pending) as stream:
        stream.write((json.dumps(manifest, ensure_ascii=True, indent=2) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    os.link(pending, directory / "manifest.json")
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="HTTPS AgentMesh origin (default: AGENTMESH_URL)")
    parser.add_argument("--backup-dir", type=Path, default=Path.home() / "agentmesh-backups")
    parser.add_argument("--tokens-file", type=Path, default=Path.home() / ".config/agentmesh/tokens.env")
    parser.add_argument("--page-limit", type=int, default=1000)
    parser.add_argument("--request-delay", type=float, default=0.55, help="minimum seconds between requests")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--allow-local-http", action="store_true", help="allow HTTP to a loopback address for tests only")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if (not math.isfinite(args.request_delay) or args.request_delay < 0
                or not math.isfinite(args.timeout) or args.timeout <= 0):
            raise BackupError("request delay must be finite and nonnegative; timeout must be finite and positive")
        settings = load_settings(args.tokens_file)
        url = validate_url(args.url or settings.get("AGENTMESH_URL", ""), args.allow_local_http)
        client = ExportClient(url, settings["ADMIN_EXPORT_TOKEN"], settings.get("CF_ACCESS_CLIENT_ID", ""),
                              settings.get("CF_ACCESS_CLIENT_SECRET", ""), args.timeout, args.request_delay)
        directory = backup(client, args.backup_dir, args.page_limit)
    except BackupError as error:
        print("Backup failed: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        print("Backup failed: local I/O or invalid export data; any incomplete run was retained", file=sys.stderr)
        return 1
    print("Backup complete: " + str(directory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
