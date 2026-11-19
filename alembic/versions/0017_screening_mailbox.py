"""Bind screening threads to a tenant's connected mailbox."""

from alembic import op

revision = "0017_screening_mailbox"
down_revision = "0016_email_screening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        SET LOCAL row_security = off;
        ALTER TABLE screening_threads ADD COLUMN mailbox_connection_id uuid;
        ALTER TABLE screening_threads ADD CONSTRAINT fk_screening_thread_mailbox
            FOREIGN KEY (tenant_id, mailbox_connection_id)
            REFERENCES mailbox_connections (tenant_id, id);
        CREATE INDEX ix_screening_thread_mailbox
            ON screening_threads (tenant_id, mailbox_connection_id);

        UPDATE screening_threads AS thread SET mailbox_connection_id = mailbox.id
        FROM mailbox_connections AS mailbox
        WHERE thread.tenant_id = mailbox.tenant_id
          AND thread.mailbox = lower(mailbox.email)
          AND mailbox.status = 'connected'
          AND mailbox.provider = 'gmail' AND mailbox.connection_method = 'imap';
    """)


def downgrade() -> None:
    raise RuntimeError("Restore the pre-upgrade backup to roll back revision 0017.")
