import importlib
from pathlib import Path

import pytest


@pytest.fixture
def client_module(monkeypatch, tmp_path):
    # Importing the client normally loads its private config.env; never inspect it.
    monkeypatch.setenv("AGENTMESH_CONFIG", str(tmp_path / "not-a-config-file"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "client" / "src"))
    return importlib.import_module("agentmesh_mcp.server")


@pytest.mark.parametrize("url", [
    "https://machine.tailnet.ts.net:8443", "https://example.com", "https://100.0.0.1",
    "http://localhost", "http://LOCALHOST:8787", "http://127.0.0.1:8787",
    "http://127.2.3.4", "http://[::1]:8787", "http://100.64.0.0",
    "http://100.100.100.100:8787", "http://100.127.255.255",
    "http://[fd7a:115c:a1e0::1]", "http://[fd7a:115c:a1e0:ffff:ffff:ffff:ffff:ffff]",
])
def test_client_url_accepts_encrypted_or_trusted_ip_addresses(client_module, url):
    assert client_module.is_allowed_url(url)


@pytest.mark.parametrize("url", [
    "http://example.com", "http://foo.ts.net", "http://localhost.example.com",
    "http://100.0.0.1", "http://100.63.255.255", "http://100.128.0.0",
    "http://192.168.1.1", "http://10.0.0.1", "http://[fd7a:115c:a1df::1]",
    "http://[fd7a:115c:a1e1::1]", "http://[::]", "http://0.0.0.0",
    "http://127.0.0.1.evil.example", "http://127.0.0.1@evil.example",
    "http://127.1", "http://2130706433", "http://0x7f000001",
    "ftp://localhost", "file://localhost/path", "", "localhost:8787",
    "https:///missing-host", "http://[::1", "http://localhost:invalid",
    "https://example.com:99999", "http://local host",
])
def test_client_url_rejects_public_http_and_invalid_urls(client_module, url):
    assert not client_module.is_allowed_url(url)


def test_client_rejects_url_before_creating_connection(client_module, monkeypatch):
    monkeypatch.setattr(client_module, "BASE_URL", "http://foo.ts.net")
    monkeypatch.setattr(client_module, "TOKEN", "test-token")
    with pytest.raises(RuntimeError, match="https"):
        client_module.client()


def test_client_url_validation_is_pure(client_module, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("URL policy must not perform DNS or network calls")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert client_module.is_allowed_url("http://100.64.0.1")
    assert not client_module.is_allowed_url("http://machine.ts.net")
