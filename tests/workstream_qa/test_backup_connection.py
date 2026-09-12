"""Offline connection-target consistency; synthetic values, no network or credentials."""

from emer.storage.backup import connection_env
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import make_url


def test_dump_restore_environment_matches_libpq_query_overrides():
    configured = (
        "postgresql+psycopg://authority.example.invalid:5432/emer"
        "?host=query.example.invalid&port=55432&sslmode=verify-full&sslrootcert=/tmp/qa-ca.pem"
    )
    admin_dsn = make_url(configured).set(database="postgres").render_as_string(hide_password=False)
    admin = conninfo_to_dict(admin_dsn.replace("postgresql+psycopg://", "postgresql://"))
    process = connection_env(configured)
    for libpq_name, env_name in [
        ("host", "PGHOST"), ("port", "PGPORT"), ("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT")
    ]:
        assert process.get(env_name) == admin[libpq_name], (
            f"{env_name} differs between isolated CREATE DATABASE and dump/restore subprocess"
        )
