"""Optional job setup, generated requirements, documents and intake lifecycle."""

from alembic import op

revision = "0012_opening_setup_lifecycle"
down_revision = "0011_mailbox_connection_methods"
branch_labels = None
depends_on = None


def dispatcher(openings: bool) -> None:
    kinds = (
        "'cv.uploaded','retention.settings_changed','retention.application_due',"
        "'retention.upload_due','mailbox.sync','mailbox.maintain','mailbox.disconnect',"
        "'mail.attachment','retention.mail_due'"
    )
    if openings:
        kinds += ",'opening.prepare'"
    op.execute(f"""
        CREATE OR REPLACE FUNCTION dispatch_outbox() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE item record; dispatched integer := 0;
        BEGIN
            FOR item IN SELECT * FROM public.outbox_messages
                WHERE dispatched_at IS NULL AND available_at <= now()
                ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 100
            LOOP
                IF item.kind IN ({kinds}) THEN
                    INSERT INTO public.job_runs
                        (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,state,attempts,available_at)
                    VALUES(gen_random_uuid(),now(),item.tenant_id,item.kind,item.aggregate_id,
                        item.dedupe_key,'queued',0,now()) ON CONFLICT(dedupe_key) DO NOTHING;
                END IF;
                UPDATE public.outbox_messages SET dispatched_at=now() WHERE id=item.id;
                dispatched := dispatched+1;
            END LOOP;
            RETURN dispatched;
        END $$;
    """)


def upgrade() -> None:
    op.execute("""
        DROP INDEX uq_one_active_opening;
        ALTER TABLE job_openings DROP CONSTRAINT job_openings_status_check;
        ALTER TABLE job_openings ADD CONSTRAINT job_openings_status_check
            CHECK (status IN ('active','paused','closed'));
        ALTER TABLE job_openings ADD COLUMN details json NOT NULL DEFAULT '{}'
                CHECK(json_typeof(details)='object'),
            ADD COLUMN questions json NOT NULL DEFAULT '[]'
                CHECK(json_typeof(questions)='array' AND json_array_length(questions)<=30),
            ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK(revision > 0),
            ADD COLUMN source_revision integer NOT NULL DEFAULT 1 CHECK(source_revision > 0),
            ADD COLUMN criteria_status text NOT NULL DEFAULT 'ready'
                CHECK(criteria_status IN ('queued','processing','ready','failed')),
            ADD COLUMN criteria_error text,
            ADD COLUMN preparation_config json NOT NULL DEFAULT '{}';
        ALTER TABLE job_criteria_versions ADD COLUMN source_revision integer,
            ADD COLUMN processing_metadata json NOT NULL DEFAULT '{}';
        ALTER TABLE job_criteria_versions DISABLE TRIGGER criteria_version_immutable;
        UPDATE job_criteria_versions SET source_revision=version;
        ALTER TABLE job_criteria_versions ENABLE TRIGGER criteria_version_immutable;
        ALTER TABLE job_criteria_versions ALTER COLUMN source_revision SET NOT NULL;
        ALTER TABLE job_criteria_versions ADD UNIQUE(opening_id, source_revision);
        UPDATE job_openings o SET source_revision=COALESCE(
            (SELECT max(v.version) FROM job_criteria_versions v WHERE v.opening_id=o.id),1);
        ALTER TABLE job_criteria ADD COLUMN source_evidence json NOT NULL DEFAULT '[]';
        CREATE TABLE opening_documents (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), opening_id uuid NOT NULL,
            file_name text NOT NULL, mime_type text NOT NULL,
            byte_length integer NOT NULL CHECK(byte_length BETWEEN 1 AND 10485760),
            sha256 text NOT NULL, content bytea NOT NULL,
            extracted_pages json NOT NULL DEFAULT '[]', extraction_method text,
            CHECK(byte_length=octet_length(content)),
            UNIQUE(tenant_id,id), UNIQUE(opening_id,sha256),
            FOREIGN KEY(tenant_id,opening_id) REFERENCES job_openings(tenant_id,id) ON DELETE
            CASCADE
        );
        CREATE TABLE opening_intake_periods (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), opening_id uuid NOT NULL,
            starts_at timestamptz NOT NULL, ends_at timestamptz,
            CHECK(ends_at IS NULL OR ends_at >= starts_at),
            FOREIGN KEY(tenant_id,opening_id) REFERENCES job_openings(tenant_id,id) ON DELETE
            CASCADE
        );
        CREATE INDEX ix_opening_intake_received
            ON opening_intake_periods(opening_id,starts_at,ends_at);
        CREATE UNIQUE INDEX uq_opening_intake_current ON opening_intake_periods(opening_id)
            WHERE ends_at IS NULL;
        INSERT INTO opening_intake_periods(id,created_at,tenant_id,opening_id,starts_at)
            SELECT gen_random_uuid(),now(),tenant_id,id,created_at FROM job_openings WHERE
            status='active';
        GRANT UPDATE ON job_openings TO recruitment_api,recruitment_worker;
        GRANT INSERT ON job_criteria_versions,job_criteria TO recruitment_worker;
        ALTER TABLE candidate_identity_reviews DROP CONSTRAINT
            candidate_identity_reviews_kind_check;
        ALTER TABLE candidate_identity_reviews ADD CONSTRAINT candidate_identity_reviews_kind_check
            CHECK(kind IN ('identity_conflict','contact_proposal',
                'contact_issue','cross_role_submission'));
        ALTER TABLE candidate_identity_reviews DROP CONSTRAINT
            candidate_identity_reviews_upload_id_key,
            ADD COLUMN target_opening_id uuid,
            ADD FOREIGN KEY(tenant_id,target_opening_id) REFERENCES job_openings(tenant_id,id),
            ADD UNIQUE(upload_id,target_opening_id);
        CREATE UNIQUE INDEX uq_identity_upload ON candidate_identity_reviews(upload_id)
            WHERE kind <> 'cross_role_submission';
    """)
    for table in ("opening_documents", "opening_intake_periods"):
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY api_isolation ON {table} TO recruitment_api
                USING(tenant_id=tenant_context() AND tenant_is_member(tenant_id))
                WITH CHECK(tenant_id=tenant_context() AND tenant_is_member(tenant_id));
            CREATE POLICY worker_isolation ON {table} TO recruitment_worker
                USING(tenant_id=tenant_context()) WITH CHECK(tenant_id=tenant_context());
            GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO recruitment_api,recruitment_worker;
        """)
    dispatcher(True)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM job_openings WHERE status='paused' OR details::text <> '{}'
                OR questions::text <> '[]' OR criteria_status <> 'ready' OR revision > 1)
                OR EXISTS(SELECT 1 FROM job_openings WHERE status='active'
                    GROUP BY tenant_id HAVING count(*)>1)
                OR EXISTS(SELECT 1 FROM opening_documents)
                OR EXISTS(SELECT 1 FROM job_criteria_versions
                    WHERE processing_metadata::text <> '{}')
                OR EXISTS(SELECT 1 FROM job_criteria WHERE source_evidence::text <> '[]')
                OR EXISTS(SELECT 1 FROM candidate_identity_reviews
                    WHERE kind='cross_role_submission')
            THEN RAISE EXCEPTION 'Preserve opening setup and lifecycle data before downgrade';
            END IF;
        END $$;
        DROP TABLE opening_intake_periods, opening_documents;
        DROP INDEX uq_identity_upload;
        ALTER TABLE candidate_identity_reviews DROP COLUMN target_opening_id, ADD UNIQUE(upload_id);
        ALTER TABLE candidate_identity_reviews DROP CONSTRAINT
            candidate_identity_reviews_kind_check;
        ALTER TABLE candidate_identity_reviews ADD CONSTRAINT candidate_identity_reviews_kind_check
            CHECK(kind IN ('identity_conflict','contact_proposal','contact_issue'));
        ALTER TABLE job_criteria DROP COLUMN source_evidence;
        ALTER TABLE job_criteria_versions DROP CONSTRAINT
            job_criteria_versions_opening_id_source_revision_key,
            DROP COLUMN source_revision, DROP COLUMN processing_metadata;
        ALTER TABLE job_openings DROP CONSTRAINT job_openings_status_check;
        ALTER TABLE job_openings DROP COLUMN details, DROP COLUMN questions, DROP COLUMN revision,
            DROP COLUMN source_revision, DROP COLUMN criteria_status, DROP COLUMN criteria_error,
            DROP COLUMN preparation_config;
        ALTER TABLE job_openings ADD CONSTRAINT job_openings_status_check CHECK(status IN
            ('active','closed'));
        CREATE UNIQUE INDEX uq_one_active_opening ON job_openings(tenant_id) WHERE status='active';
        REVOKE UPDATE ON job_openings FROM recruitment_api,recruitment_worker;
        REVOKE INSERT ON job_criteria_versions,job_criteria FROM recruitment_worker;
    """)
    dispatcher(False)
