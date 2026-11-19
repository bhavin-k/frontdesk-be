"""Cache tenant JD checklists for deterministic coverage scoring."""

from alembic import op

revision = "0018_consistent_scoring"
down_revision = "0017_screening_mailbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE job_scoring_rubrics (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            signature text NOT NULL, content json NOT NULL,
            model text NOT NULL, prompt_version text NOT NULL,
            UNIQUE (tenant_id, signature)
        );
        ALTER TABLE job_scoring_rubrics ENABLE ROW LEVEL SECURITY;
        ALTER TABLE job_scoring_rubrics FORCE ROW LEVEL SECURITY;
        CREATE POLICY api_isolation ON job_scoring_rubrics TO recruitment_api
            USING (tenant_id=tenant_context() AND tenant_is_member(tenant_id))
            WITH CHECK (tenant_id=tenant_context() AND tenant_is_member(tenant_id));
        CREATE POLICY worker_isolation ON job_scoring_rubrics TO recruitment_worker
            USING (tenant_id=tenant_context()) WITH CHECK (tenant_id=tenant_context());
        GRANT SELECT, INSERT ON job_scoring_rubrics TO recruitment_api, recruitment_worker;
    """)


def downgrade() -> None:
    raise RuntimeError("Restore the pre-upgrade backup to roll back revision 0018.")
