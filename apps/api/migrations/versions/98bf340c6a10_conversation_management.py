"""Owned conversation management, finite recap work, and explicit share snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "98bf340c6a10"
down_revision = "3a5298410c51"
branch_labels = None
depends_on = None


def upgrade():
    columns = [
        sa.Column("title_origin", sa.String(10), nullable=False, server_default="auto"),
        sa.Column("title_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activity_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_status", sa.String(12), nullable=False, server_default="idle"),
        sa.Column("summary_error", sa.String(240), nullable=True),
        sa.Column("summary_input_hash", sa.String(64), nullable=True),
        sa.Column("summary_job_id", sa.String(36), nullable=True),
        sa.Column("summary_fence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_owner", sa.String(36), nullable=True),
        sa.Column("summary_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    ]
    for column in columns:
        op.add_column("conversations", column)
    op.execute("""
        UPDATE conversations c SET updated_at = GREATEST(c.created_at,
          COALESCE((SELECT MAX(COALESCE(r.completed_at,r.created_at)) FROM runs r
                    WHERE r.conversation_id=c.id), c.created_at),
          COALESCE((SELECT MAX(l.created_at) FROM live_sessions l
                    WHERE l.conversation_id=c.id), c.created_at))
    """)
    op.create_index("ix_conversations_owned_activity", "conversations",
                    ["session_id", "pinned", "updated_at", "id"])
    op.add_column("live_sessions", sa.Column("answer_pending", sa.Boolean(), nullable=False,
                                            server_default=sa.false()))
    op.add_column("live_sessions", sa.Column("answer_pending_until", sa.DateTime(timezone=True)))
    op.add_column("provider_attempts", sa.Column("conversation_id", sa.String(36)))
    op.add_column("provider_attempts", sa.Column("metadata_job_id", sa.String(36)))
    op.create_foreign_key("fk_provider_attempt_conversation", "provider_attempts", "conversations",
                          ["conversation_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_provider_attempts_conversation_id", "provider_attempts", ["conversation_id"])
    op.create_index("ix_provider_attempts_metadata_job_id", "provider_attempts", ["metadata_job_id"])
    op.create_table(
        "conversation_shares",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("browser_sessions.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_conversation_shares_session_id", "conversation_shares", ["session_id"])


def downgrade():
    op.drop_table("conversation_shares")
    op.drop_column("provider_attempts", "metadata_job_id")
    op.drop_column("provider_attempts", "conversation_id")
    op.drop_column("live_sessions", "answer_pending_until")
    op.drop_column("live_sessions", "answer_pending")
    op.drop_index("ix_conversations_owned_activity", table_name="conversations")
    for name in (
        "summary_lease_expires_at", "summary_owner", "summary_requested_at", "summary_attempts",
        "summary_fence", "summary_job_id", "summary_input_hash", "summary_error", "summary_status",
        "summary", "activity_version", "updated_at", "pinned", "title_generated", "title_revision",
        "title_origin",
    ):
        op.drop_column("conversations", name)
