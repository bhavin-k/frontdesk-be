"""Audited contact corrections, identity cases and source-ordered CV publication."""

from alembic import op

revision = "0008_candidate_contact_review"
down_revision = "0007_mail_intake"
branch_labels = None
depends_on = None

TABLES = ("candidate_contact_revisions", "candidate_identity_reviews")


def upgrade() -> None:
    op.execute("""
        ALTER TABLE candidates ADD COLUMN current_source_at timestamptz,
            ADD COLUMN current_source_key text, ADD COLUMN contact_revision integer NOT NULL
            DEFAULT 1
                CHECK(contact_revision > 0), ADD COLUMN contact_overrides json NOT NULL DEFAULT
                '{}';
        ALTER TABLE cv_uploads ADD COLUMN source_received_at timestamptz,
            ADD COLUMN resolved_candidate_id uuid, ADD COLUMN identity_choice text;
        UPDATE cv_uploads SET source_received_at = created_at;
        ALTER TABLE cv_uploads ALTER COLUMN source_received_at SET NOT NULL;
        ALTER TABLE cv_uploads ADD FOREIGN KEY(tenant_id,resolved_candidate_id)
            REFERENCES candidates(tenant_id,id);
        ALTER TABLE applications ADD COLUMN current_resume_id uuid;
        ALTER TABLE applications ADD CONSTRAINT fk_current_resume
            FOREIGN KEY(tenant_id,id,current_resume_id) REFERENCES
            resumes(tenant_id,application_id,id)
            DEFERRABLE INITIALLY DEFERRED;
        UPDATE applications a SET current_resume_id = (
            SELECT r.id FROM resumes r JOIN cv_uploads u ON u.id=r.upload_id
            WHERE r.application_id=a.id ORDER BY u.source_received_at DESC,u.id DESC LIMIT 1
        );
        UPDATE candidates c SET current_source_at = x.source_received_at, current_source_key =
        x.id::text
            FROM (SELECT DISTINCT ON(a.candidate_id) a.candidate_id,u.source_received_at,u.id
                FROM applications a JOIN cv_uploads u ON u.application_id=a.id
                ORDER BY a.candidate_id,u.source_received_at DESC,u.id DESC) x
            WHERE c.id=x.candidate_id;
        CREATE TABLE candidate_contact_revisions (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), candidate_id uuid NOT NULL,
            revision integer NOT NULL CHECK(revision > 0), actor_id uuid NOT NULL REFERENCES
            users(id),
            before json NOT NULL, after json NOT NULL, reason text NOT NULL CHECK(length(reason)>0),
            UNIQUE(candidate_id,revision),
            FOREIGN KEY(tenant_id,candidate_id) REFERENCES candidates(tenant_id,id) ON DELETE
            CASCADE
        );
        CREATE TABLE candidate_identity_reviews (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), upload_id uuid UNIQUE, message_id uuid,
            candidate_id uuid, kind text NOT NULL CHECK(kind IN
            ('identity_conflict','contact_proposal','contact_issue')),
            status text NOT NULL CHECK(status IN ('open','resolved')), proposed_contacts json
            NOT NULL,
            candidate_options json NOT NULL, decision text, reason text,
            actor_id uuid REFERENCES users(id), resolved_at timestamptz,
            FOREIGN KEY(tenant_id,upload_id) REFERENCES cv_uploads(tenant_id,id) ON DELETE CASCADE,
            FOREIGN KEY(tenant_id,message_id) REFERENCES mail_source_messages(tenant_id,id) ON
            DELETE CASCADE,
            FOREIGN KEY(tenant_id,candidate_id) REFERENCES candidates(tenant_id,id) ON DELETE
            CASCADE,
            CHECK(upload_id IS NOT NULL OR message_id IS NOT NULL)
        );
        GRANT UPDATE (profile,contact_revision,contact_overrides) ON candidates TO recruitment_api;
        GRANT INSERT ON candidates, candidate_contacts TO recruitment_api;
        GRANT UPDATE, DELETE ON candidate_contacts TO recruitment_api;
        GRANT UPDATE (resolved_candidate_id,identity_choice) ON cv_uploads TO recruitment_api;
    """)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY api_isolation ON {table} TO recruitment_api
            USING (tenant_id = tenant_context() AND tenant_is_member(tenant_id))
            WITH CHECK (tenant_id = tenant_context() AND tenant_is_member(tenant_id))""")
        op.execute(f"""CREATE POLICY worker_isolation ON {table} TO recruitment_worker
            USING (tenant_id = tenant_context()) WITH CHECK (tenant_id = tenant_context())""")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO recruitment_api")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO recruitment_worker")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN IF EXISTS(SELECT 1 FROM candidate_contact_revisions)
            OR EXISTS(SELECT 1 FROM candidate_identity_reviews) THEN
            RAISE EXCEPTION 'Export contact history and identity cases before downgrade';
        END IF; END $$;
    """)
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("""
        REVOKE UPDATE (resolved_candidate_id,identity_choice) ON cv_uploads FROM recruitment_api;
        REVOKE UPDATE (profile,contact_revision,contact_overrides) ON candidates FROM
        recruitment_api;
        REVOKE INSERT ON candidates,candidate_contacts FROM recruitment_api;
        REVOKE UPDATE,DELETE ON candidate_contacts FROM recruitment_api;
        ALTER TABLE applications DROP CONSTRAINT fk_current_resume, DROP COLUMN current_resume_id;
        ALTER TABLE cv_uploads DROP COLUMN source_received_at, DROP COLUMN resolved_candidate_id,
            DROP COLUMN identity_choice;
        ALTER TABLE candidates DROP COLUMN current_source_at, DROP COLUMN current_source_key,
            DROP COLUMN contact_revision, DROP COLUMN contact_overrides;
    """)
