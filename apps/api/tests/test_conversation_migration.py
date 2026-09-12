"""Upgrade, downgrade and model parity in a disposable isolated database only."""

import os
import subprocess
import sys
from uuid import uuid4

import psycopg


def test_additive_migration_preserves_old_titles_and_has_no_model_drift():
    name = "emer_metadata_migration_" + uuid4().hex[:12]
    admin_url = "postgresql://localhost:55432/postgres"
    dsn = f"postgresql://localhost:55432/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    env = {
        **os.environ, "DATABASE_URL": f"postgresql+psycopg://localhost:55432/{name}",
        "OPENROUTER_API_KEY": "", "OPENAI_API_KEY": "",
    }

    def alembic(*args):
        result = subprocess.run([sys.executable, "-m", "alembic", *args], env=env,
                                capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stdout + result.stderr

    owner, generic, named, automatic, voice, run_id = [str(uuid4()) for _ in range(6)]
    question = "Which cancellation policy applies? " + "Please explain the recorded policy and uncertainty. " * 3
    try:
        alembic("upgrade", "3a5298410c51")
        with psycopg.connect(dsn) as db:
            db.execute("INSERT INTO browser_sessions(id,token_hash,created_at,expires_at,revoked) "
                       "VALUES (%s,'fixture','2026-01-01','2030-01-01',false)", (owner,))
            db.execute("INSERT INTO corpus_indexes(id,checksum,corpus_checksum,bundle,manifest,created_at) "
                       "VALUES ('fixture','fixture','fixture','fixture','{}','2026-01-01')")
            for cid, title in [
                (generic, "New conversation"), (named, "A custom title"), (automatic, question[:120]),
                (voice, "Voice conversation"),
            ]:
                db.execute("INSERT INTO conversations(id,session_id,title,context_version,created_at) "
                           "VALUES (%s,%s,%s,1,'2026-01-01')", (cid, owner, title))
            db.execute(
                """INSERT INTO runs
                (id,session_id,conversation_id,index_id,idempotency_key,body_hash,question,context_version,
                 context,status,metrics,cancel_requested,fence,attempt_started,created_at,completed_at)
                 VALUES (%s,%s,%s,'fixture','fixture','fixture',%s,1,'{}',
                         'completed','{}',false,1,false,'2026-01-02','2026-01-03')""",
                (run_id, owner, automatic, question),
            )
        alembic("upgrade", "head")
        alembic("check")
        with psycopg.connect(dsn) as db:
            rows = {row[0]: row[1:] for row in db.execute(
                "SELECT id,title,title_origin,summary_status,summary_job_id,updated_at FROM conversations"
            ).fetchall()}
            assert all(row[1] == "auto" for row in rows.values())
            assert rows[named][0] == "A custom title"
            assert rows[voice][0] == "Voice conversation"
            assert rows[automatic][0] == question[:120]
            assert str(rows[automatic][4]).startswith("2026-01-03")
            assert all(row[2:4] == ("idle", None) for row in rows.values())
        alembic("downgrade", "3a5298410c51")
        alembic("upgrade", "head")
        alembic("check")
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
