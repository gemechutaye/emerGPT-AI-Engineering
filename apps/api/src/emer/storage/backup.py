"""Maintainer-only, consistent Postgres backups; restore only into a new isolated database."""

import json
import os
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import psycopg
from emer.services.ingestion import Bundle
from emer.settings import settings
from emer.storage.models import Base
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import Conninfo
from sqlalchemy.engine import make_url

APP_TABLES = tuple(sorted([*Base.metadata.tables, "alembic_version"]))


def connection_parameters(database_url: str, database: str | None = None) -> dict[str, str]:
    url = make_url(database_url).set(drivername="postgresql")
    parameters = conninfo_to_dict(url.render_as_string(hide_password=False))
    if database is not None:
        parameters["dbname"] = database
    return parameters


def connection_env(database_url: str, database: str | None = None) -> dict[str, str]:
    # Parse with libpq so query host/port and SSL options have the same precedence
    # as psycopg. Keep credentials in the private child environment, not argv.
    names = {
        item.keyword.decode(): item.envvar.decode()
        for item in Conninfo.get_defaults() if item.envvar
    }
    parameters = connection_parameters(database_url, database)
    unsupported = sorted(parameters.keys() - names.keys())
    if unsupported:
        raise ValueError("Backup/restore cannot transfer connection options: " + ", ".join(unsupported))
    return {**os.environ, **{names[key]: value for key, value in parameters.items()}}


def backup(destination: str) -> dict:
    path = Path(destination).resolve()
    if path.exists():
        raise ValueError("Backup destination already exists; choose a new filename")
    if not str(path).startswith(str(Path(".local").resolve()) + os.sep):
        raise ValueError("Store backups under .local/ outside version control")
    env = connection_env(settings.database_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mask = os.umask(0o077)
    try:
        subprocess.run(
            [
                "pg_dump", "--format=custom", "--no-owner", "--no-acl", "--strict-names",
                *[f"--table=public.{name}" for name in APP_TABLES],
                "--file", str(path),
            ],
            env=env,
            check=True,
            capture_output=True,
        )
    finally:
        os.umask(old_mask)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "file": str(path),
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "format": "pg_dump custom",
        "tables": list(APP_TABLES),
        "includes": "All app tables, exact source/index bundles, run/evidence/history/draft/live records",
    }
    manifest_path = path.with_suffix(path.suffix + ".json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    manifest_path.chmod(0o600)
    return manifest


def restore(source: str, database: str) -> dict:
    if not database.startswith("emer_restore_") or not database.replace("_", "").isalnum():
        raise ValueError("Restore database must be a new emer_restore_* name")
    path = Path(source).resolve()
    manifest = json.loads(path.with_suffix(path.suffix + ".json").read_text())
    if sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Backup checksum mismatch")
    destination_url = settings.restore_database_url or settings.database_url
    # Reject unsupported transfer options before CREATE DATABASE. Explicit
    # target names override a query-string dbname as well as the URL path.
    env = connection_env(destination_url, database)
    with psycopg.connect(**connection_parameters(destination_url, "postgres"), autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    subprocess.run(
        ["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "--dbname", database, str(path)],
        env=env,
        check=True,
        capture_output=True,
    )
    with psycopg.connect(**connection_parameters(destination_url, database)) as db:
        bundles = db.execute("SELECT id,checksum,bundle FROM corpus_indexes").fetchall()
        loaded = {row[0]: Bundle.from_bytes(bytes(row[2]), row[1]) for row in bundles}
        answers = db.execute("SELECT index_id,answer FROM runs WHERE status='completed'").fetchall()
        table_counts = {
            name: db.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("public", name))
            ).fetchone()[0]
            for name in APP_TABLES
        }
        citations = 0
        for index_id, answer in answers:
            docs = {doc.doc_id: doc for doc in loaded[index_id].documents}
            for statement in answer["statements"] + answer["next_steps"]:
                for citation in statement["citations"]:
                    assert (
                        docs[citation["doc_id"]].text[citation["start"] : citation["end"]]
                        == citation["quote"]
                    )
                    citations += 1
    return {
        "database": database,
        "bundles_verified": len(loaded),
        "completed_answers": len(answers),
        "historical_citations_verified": citations,
        "table_counts": table_counts,
        "classification": "real Postgres restore, exact source span verification",
    }
