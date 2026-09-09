"""Shared aiohttp session factory for Windows-safe DNS.

aiohttp 3.10+ defaults to ``AsyncResolver`` (c-ares / aiodns) when that
package is installed. On Windows home-router DNS the c-ares path frequently
raises ``Timeout while contacting DNS servers`` even though ``nslookup`` and
the OS resolver succeed — and IPv6 AAAA lookups make it worse.

Every outbound REST client in this project should go through
:func:`make_client_session` so we:

  * force ``ThreadedResolver`` (Windows ``getaddrinfo``)
  * stick to IPv4 (``AF_INET``) to skip hanging AAAA queries
  * keep a 5-minute DNS cache on the connector

Must be called from a running event loop (``ThreadedResolver`` requires it).
"""

from __future__ import annotations

import socket
from typing import Any, Mapping, Optional

import aiohttp
from aiohttp.resolver import ThreadedResolver

DEFAULT_DNS_CACHE_SEC = 300


def make_tcp_connector(**kwargs: Any) -> aiohttp.TCPConnector:
    """IPv4 + threaded-DNS connector. Call from a running event loop."""
    kwargs.setdefault("family", socket.AF_INET)
    kwargs.setdefault("ttl_dns_cache", DEFAULT_DNS_CACHE_SEC)
    kwargs.setdefault("use_dns_cache", True)
    kwargs.setdefault("enable_cleanup_closed", True)
    if "resolver" not in kwargs:
        kwargs["resolver"] = ThreadedResolver()
    return aiohttp.TCPConnector(**kwargs)


def make_client_session(
    *,
    timeout: Optional[aiohttp.ClientTimeout] = None,
    headers: Optional[Mapping[str, str]] = None,
    connector: Optional[aiohttp.TCPConnector] = None,
    **connector_kw: Any,
) -> aiohttp.ClientSession:
    """Client session using :func:`make_tcp_connector` unless *connector* is given."""
    conn = connector or make_tcp_connector(**connector_kw)
    return aiohttp.ClientSession(
        connector=conn,
        timeout=timeout or aiohttp.ClientTimeout(total=30),
        headers=headers,
    )
