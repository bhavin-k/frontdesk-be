"""Retained mailbox thread references for audited reply and bounce proposals."""

from alembic import op

revision = "0010_mail_thread_correlation"
down_revision = "0009_phase2_jobs_rag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE mail_source_messages ADD COLUMN provider_thread_id text")
    op.execute(
        "CREATE INDEX ix_mail_message_thread "
        "ON mail_source_messages(connection_id,provider_thread_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_mail_message_thread")
    op.execute("ALTER TABLE mail_source_messages DROP COLUMN provider_thread_id")
