"""Shared, network-free URL policy for the client and setup wizard."""

import ipaddress
from urllib.parse import urlsplit


def is_allowed_url(value: str) -> bool:
    """Allow HTTPS anywhere, or HTTP only on loopback and Tailscale IP ranges.

    Tailnet DNS names still require HTTPS: resolving a name here would make the
    decision depend on DNS and could send a token outside the tailnet later.
    """
    try:
        url = urlsplit(value)
        host = url.hostname
        _ = url.port  # Reject invalid ports before a client can use the URL.
        if not host or any(character.isspace() for character in host):
            return False
        if url.scheme == "https":
            return True
        if url.scheme != "http":
            return False
        if host == "localhost":
            return True
        address = ipaddress.ip_address(host)
        return address.is_loopback or any(address in network for network in (
            ipaddress.ip_network("100.64.0.0/10"),
            ipaddress.ip_network("fd7a:115c:a1e0::/48"),
        ))
    except ValueError:
        return False
