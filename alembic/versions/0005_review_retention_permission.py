"""Repair the recruiter retention grant on earlier Phase 1 installations."""

from alembic import op

revision = "0005_review_retention_permission"
down_revision = "0004_better_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Decisions update the application and its upload deadline in the same transaction.
    # Some existing 0004 databases predate the column grant in the reviewed baseline.
    op.execute("GRANT UPDATE (retention_expires_at) ON cv_uploads TO recruitment_api")


def downgrade() -> None:
    # Preserve the narrow grant: the reviewed 0002 baseline already requires it.
    # Revoking it here would break decisions on otherwise valid 0004 databases.
    pass
