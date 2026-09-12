"""Actual PostgreSQL export/restore, isolated databases, no provider calls."""

import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from emer.settings import settings
from emer.storage.backup import APP_TABLES, backup, restore
from emer.storage.models import Base
from psycopg import sql
from sqlalchemy import create_engine


@pytest.fixture(params=["direct", "query_overrides"])
def backup_database(monkeypatch, tmp_path, request):
    names = ["emer_backup_test_" + uuid4().hex[:12], "emer_restore_" + uuid4().hex[:12]]
    admin_url = "postgresql://localhost:55432/postgres"
    with psycopg.connect(admin_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(names[0])))
    url = f"postgresql+psycopg://localhost:55432/{names[0]}"
    engine = create_engine(url)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as db:
            db.exec_driver_sql("CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY)")
            db.exec_driver_sql("INSERT INTO alembic_version VALUES ('test-checkpoint')")
            db.exec_driver_sql("CREATE TABLE unrelated_public_data(secret text)")
            db.exec_driver_sql("CREATE SCHEMA managed_platform")
            db.exec_driver_sql("CREATE TABLE managed_platform.internal_data(secret text)")
        monkeypatch.setattr(settings, "database_url", url)
        target = "postgresql+psycopg://localhost:55432/postgres"
        if request.param == "query_overrides":
            target = (
                "postgresql+psycopg://unavailable.invalid:1/ignored"
                "?host=localhost&port=55432&dbname=also_ignored&sslmode=disable"
            )
        monkeypatch.setattr(settings, "restore_database_url", target)
        monkeypatch.chdir(tmp_path)
        yield names[1]
    finally:
        engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as db:
            for name in names:
                db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_export_only_app_tables_and_restore_on_separate_destination(backup_database, monkeypatch):
    manifest = backup(".local/checkpoint.dump")
    assert manifest["tables"] == list(APP_TABLES)
    assert Path(manifest["file"]).stat().st_mode & 0o777 == 0o600
    assert Path(manifest["file"] + ".json").stat().st_mode & 0o777 == 0o600
    # The source may be hosted or unavailable during disaster recovery.
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://unavailable.invalid/source")
    result = restore(manifest["file"], backup_database)
    assert result["table_counts"]["alembic_version"] == 1
    assert result["completed_answers"] == 0
    with psycopg.connect(f"postgresql://localhost:55432/{backup_database}") as db:
        assert db.execute("SELECT to_regclass('public.unrelated_public_data')").fetchone()[0] is None
        assert db.execute("SELECT to_regnamespace('managed_platform')").fetchone()[0] is None
    # A second restore must fail before overwriting a verified destination.
    with pytest.raises(psycopg.errors.DuplicateDatabase):
        restore(manifest["file"], backup_database)


def test_tampered_backup_never_creates_destination(backup_database):
    manifest = backup(".local/checkpoint.dump")
    Path(manifest["file"]).write_bytes(b"not the recorded database archive")
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore(manifest["file"], backup_database)
    with psycopg.connect("postgresql://localhost:55432/postgres") as db:
        assert db.execute("SELECT count(*) FROM pg_database WHERE datname=%s", (backup_database,)).fetchone()[0] == 0
    assert json.loads(Path(manifest["file"] + ".json").read_text())["sha256"] == manifest["sha256"]
