"""Operational failure boundaries using a private unavailable database endpoint.

The real serving database and provider configuration are untouched. These tests use
ASGI routing and a genuine refused PostgreSQL connection, not a missing-knowledge fixture.
"""

import socket

import httpx
import pytest
from emer.api import app as api
from emer.storage import bundles
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/health/ready",
        "/api/v1/corpus",
        "/api/v1/sources/PT-004",
        "/api/v1/sources/PT-004?index_id=private-unavailable-index",
    ],
)
async def test_unavailable_database_returns_operational_503_not_knowledge_absence(monkeypatch, path):
    # Reserve a local port without listening. It cannot accidentally connect to another
    # process's database, and a connection fails promptly without stopping any service.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1]
        engine = create_async_engine(
            f"postgresql+psycopg://fixture:fixture@127.0.0.1:{port}/unavailable",
            connect_args={"connect_timeout": 1},
        )
        session = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(api, "Session", session)
        monkeypatch.setattr(bundles, "Session", session)
        monkeypatch.setattr(bundles, "_cache", {})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False),
                base_url="http://database-failure.test",
            ) as client:
                response = await client.get(path)
            assert response.status_code == 503, response.text
            assert response.json() == {
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "Saved work is temporarily unavailable. Please try again.",
                    "retryable": True,
                }
            }
            assert response.headers["X-Request-ID"]
            assert "answer" not in response.json() and "sources" not in response.json()
            assert "127.0.0.1" not in response.text and "psycopg" not in response.text
        finally:
            await engine.dispose()
