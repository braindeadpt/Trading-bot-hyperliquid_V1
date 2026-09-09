"""Windows-safe aiohttp session factory (threaded DNS + IPv4)."""

from __future__ import annotations

import os
import socket
import sys

import pytest
from aiohttp.resolver import ThreadedResolver

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.exchanges.funding_aggregator import FundingOIAggregator
from src.utils.http import make_client_session, make_tcp_connector

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_connector_uses_threaded_resolver_and_ipv4() -> None:
    connector = make_tcp_connector()
    try:
        assert connector.family == socket.AF_INET
        assert isinstance(connector._resolver, ThreadedResolver)
        assert connector.use_dns_cache is True
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_client_session_owns_ipv4_connector() -> None:
    session = make_client_session()
    try:
        assert session.connector is not None
        assert session.connector.family == socket.AF_INET
        assert isinstance(session.connector._resolver, ThreadedResolver)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_funding_aggregator_reuses_http_session() -> None:
    agg = FundingOIAggregator()
    try:
        first = await agg._ensure_session()
        second = await agg._ensure_session()
        assert first is second
        assert first.connector is not None
        assert first.connector.family == socket.AF_INET
    finally:
        await agg.close()
    assert agg._session is None
