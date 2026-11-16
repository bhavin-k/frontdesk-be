"""Original CV bytes, immutable criteria, identity, evidence and human decisions."""

from alembic import op

revision = "0002_cv_matching"
down_revision = "0001_identity_tenancy"
branch_labels = None
depends_on = None

TABLES = (
    "job_openings",
    "job_criteria_versions",
    "job_criteria",
    "candidates",
    "candidate_contacts",
    "resume_files",
    "applications",
    "cv_uploads",
    "resumes",
    "resume_extractions",
    "resume_chunks",
    "match_runs",
    "criterion_results",
    "review_decisions",
)


def upgrade() -> None:
    op.execute("""
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE job_openings (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), title varchar(200) NOT NULL,
            job_description text NOT NULL, status text NOT NULL CHECK(status IN ('active',
                'closed')),
            UNIQUE(tenant_id, id)
        );
        CREATE UNIQUE INDEX uq_one_active_opening ON job_openings(tenant_id) WHERE status =
            'active';
        CREATE TABLE job_criteria_versions (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), opening_id uuid NOT NULL,
            version integer NOT NULL CHECK(version > 0),
            shortlist_threshold numeric(5,2) NOT NULL CHECK(shortlist_threshold BETWEEN 0 AND 100),
            UNIQUE(tenant_id, id), UNIQUE(opening_id, version),
            FOREIGN KEY (tenant_id, opening_id) REFERENCES job_openings(tenant_id, id) ON DELETE
                CASCADE
        );
        CREATE TABLE job_criteria (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), version_id uuid NOT NULL,
            position integer NOT NULL, text text NOT NULL, required boolean NOT NULL,
            strict boolean NOT NULL CHECK(NOT strict OR required),
            weight numeric(8,3) NOT NULL CHECK(weight > 0),
            embedding vector(1536), embedding_model text,
            UNIQUE(tenant_id, id), UNIQUE(version_id, position),
            FOREIGN KEY (tenant_id, version_id)
                REFERENCES job_criteria_versions(tenant_id, id) ON DELETE CASCADE
        );
        CREATE TABLE candidates (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), profile json NOT NULL,
            UNIQUE(tenant_id, id)
        );
        CREATE TABLE candidate_contacts (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), candidate_id uuid NOT NULL,
            kind text NOT NULL CHECK(kind IN ('email', 'mobile')), value text NOT NULL,
            UNIQUE(tenant_id, kind, value),
            UNIQUE(tenant_id, candidate_id, kind),
            FOREIGN KEY (tenant_id, candidate_id) REFERENCES candidates(tenant_id, id) ON DELETE
                CASCADE
        );
        CREATE TABLE resume_files (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), sha256 varchar(64) NOT NULL,
            content bytea NOT NULL, mime_type text NOT NULL,
            byte_length integer NOT NULL CHECK(byte_length BETWEEN 1 AND 10485760),
            CHECK(octet_length(content) = byte_length),
            CHECK(mime_type IN ('application/pdf',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
            UNIQUE(tenant_id, id), UNIQUE(tenant_id, sha256)
        );
        CREATE TABLE applications (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), candidate_id uuid NOT NULL,
            opening_id uuid NOT NULL, status text NOT NULL
                CHECK(status IN ('processing', 'needs_review', 'shortlisted', 'rejected')),
            latest_match_id uuid, last_activity_at timestamptz NOT NULL, rejected_at timestamptz,
            retention_expires_at timestamptz NOT NULL, lock_version integer NOT NULL
                CHECK(lock_version > 0),
            CHECK((status = 'rejected') = (rejected_at IS NOT NULL)),
            UNIQUE(tenant_id, id),
            FOREIGN KEY (tenant_id, candidate_id) REFERENCES candidates(tenant_id, id) ON DELETE
                CASCADE,
            FOREIGN KEY (tenant_id, opening_id) REFERENCES job_openings(tenant_id, id)
        );
        CREATE UNIQUE INDEX uq_active_application ON applications(tenant_id, candidate_id)
            WHERE rejected_at IS NULL;
        CREATE INDEX ix_application_expiry ON applications(tenant_id, retention_expires_at);
        CREATE TABLE cv_uploads (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), file_id uuid NOT NULL UNIQUE,
            opening_id uuid NOT NULL, application_id uuid, status text NOT NULL
                CHECK(status IN ('queued','extracting','profiling','embedding','matching',
                    'retry_wait','completed','needs_review')),
            review_reason text, retention_expires_at timestamptz NOT NULL,
            UNIQUE(tenant_id, id),
            FOREIGN KEY (tenant_id, file_id) REFERENCES resume_files(tenant_id, id) ON DELETE
                CASCADE,
            FOREIGN KEY (tenant_id, opening_id) REFERENCES job_openings(tenant_id, id),
            FOREIGN KEY (tenant_id, application_id) REFERENCES applications(tenant_id, id) ON
                DELETE CASCADE
        );
        CREATE INDEX ix_upload_review ON cv_uploads(tenant_id, status, created_at);
        CREATE TABLE resumes (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), application_id uuid NOT NULL,
            upload_id uuid NOT NULL UNIQUE, version integer NOT NULL CHECK(version > 0),
            UNIQUE(tenant_id, id), UNIQUE(application_id, version),
            UNIQUE(tenant_id, application_id, id),
            FOREIGN KEY (tenant_id, application_id) REFERENCES applications(tenant_id, id) ON
                DELETE CASCADE,
            FOREIGN KEY (tenant_id, upload_id) REFERENCES cv_uploads(tenant_id, id) ON DELETE
                CASCADE
        );
        CREATE TABLE resume_extractions (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), upload_id uuid NOT NULL UNIQUE,
            pages json NOT NULL, method text NOT NULL, confidence numeric(5,4) NOT NULL
                CHECK(confidence BETWEEN 0 AND 1),
            profile json NOT NULL, profile_model text NOT NULL, prompt_version text NOT NULL,
            FOREIGN KEY (tenant_id, upload_id) REFERENCES cv_uploads(tenant_id, id) ON DELETE
                CASCADE
        );
        CREATE TABLE resume_chunks (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), resume_id uuid NOT NULL,
            position integer NOT NULL, source_spans json NOT NULL,
            text text NOT NULL, scoring_text text NOT NULL,
            token_start integer NOT NULL, token_end integer NOT NULL CHECK(token_end > token_start),
            embedding vector(1536) NOT NULL, embedding_model text NOT NULL,
            embedding_dimensions integer NOT NULL CHECK(embedding_dimensions = 1536),
            UNIQUE(tenant_id, id), UNIQUE(resume_id, position),
            FOREIGN KEY (tenant_id, resume_id) REFERENCES resumes(tenant_id, id) ON DELETE CASCADE
        );
        CREATE INDEX ix_chunks_resume ON resume_chunks(tenant_id, resume_id);
        CREATE INDEX ix_chunk_vector ON resume_chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        CREATE TABLE match_runs (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), application_id uuid NOT NULL,
            resume_id uuid NOT NULL, criteria_version_id uuid NOT NULL,
            recommendation text NOT NULL CHECK(recommendation IN
                ('recommended_shortlist','recommended_rejection','needs_review')),
            score numeric(5,2) CHECK(score BETWEEN 0 AND 100), reason text NOT NULL,
            model text NOT NULL, provider text NOT NULL, prompt_version text NOT NULL,
            embedding_model text NOT NULL, embedding_dimensions integer NOT NULL
                CHECK(embedding_dimensions = 1536),
            missing_required_criteria json NOT NULL, processed_at timestamptz NOT NULL,
            UNIQUE(tenant_id, id), UNIQUE(resume_id, criteria_version_id),
            UNIQUE(tenant_id, application_id, id),
            FOREIGN KEY (tenant_id, application_id) REFERENCES applications(tenant_id, id) ON
                DELETE CASCADE,
            FOREIGN KEY (tenant_id, application_id, resume_id)
                REFERENCES resumes(tenant_id, application_id, id) ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, criteria_version_id) REFERENCES
                job_criteria_versions(tenant_id, id)
        );
        ALTER TABLE applications ADD CONSTRAINT fk_latest_match
            FOREIGN KEY (tenant_id, id, latest_match_id)
                REFERENCES match_runs(tenant_id, application_id, id)
            DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE criterion_results (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), match_id uuid NOT NULL,
            criterion_id uuid NOT NULL, status text NOT NULL
                CHECK(status IN ('met','partial','missing','unknown')),
            confidence numeric(5,4) NOT NULL CHECK(confidence BETWEEN 0 AND 1),
            reason text NOT NULL, evidence json NOT NULL,
            UNIQUE(match_id, criterion_id),
            FOREIGN KEY (tenant_id, match_id) REFERENCES match_runs(tenant_id, id) ON DELETE
                CASCADE,
            FOREIGN KEY (tenant_id, criterion_id) REFERENCES job_criteria(tenant_id, id)
        );
        CREATE TABLE review_decisions (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), application_id uuid NOT NULL,
            match_id uuid NOT NULL, recruiter_id uuid NOT NULL REFERENCES users(id),
            decision text NOT NULL CHECK(decision IN ('shortlist', 'reject')), reason text NOT NULL,
            FOREIGN KEY (tenant_id, application_id) REFERENCES applications(tenant_id, id) ON
                DELETE CASCADE,
            FOREIGN KEY (tenant_id, application_id, match_id)
                REFERENCES match_runs(tenant_id, application_id, id) ON DELETE CASCADE
        );
        CREATE FUNCTION protect_rejection() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.status = 'rejected'
               AND current_user IN ('recruitment_api', 'recruitment_worker')
               AND (NEW.status IS DISTINCT FROM OLD.status
                    OR NEW.rejected_at IS DISTINCT FROM OLD.rejected_at
                    OR NEW.latest_match_id IS DISTINCT FROM OLD.latest_match_id) THEN
                RAISE EXCEPTION 'Confirmed rejection is immutable' USING ERRCODE = '42501';
            END IF;
            IF NEW.status = 'rejected' AND
               (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status) THEN
                IF current_user <> 'recruitment_api' OR NOT public.tenant_is_member(NEW.tenant_id)
                   OR NOT EXISTS (SELECT 1 FROM public.review_decisions
                      WHERE application_id = NEW.id AND match_id = NEW.latest_match_id
                      AND recruiter_id = public.actor_context() AND decision = 'reject'
                      AND created_at >= transaction_timestamp()) THEN
                    RAISE EXCEPTION 'Explicit recruiter decision required' USING ERRCODE = '42501';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER application_rejection_guard BEFORE INSERT OR UPDATE ON applications
            FOR EACH ROW EXECUTE FUNCTION protect_rejection();
        CREATE FUNCTION immutable_criteria() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Published criteria are immutable' USING ERRCODE = '23514';
        END $$;
        CREATE TRIGGER criteria_version_immutable BEFORE UPDATE ON job_criteria_versions
            FOR EACH ROW EXECUTE FUNCTION immutable_criteria();
        GRANT USAGE ON SCHEMA public TO recruitment_api, recruitment_worker;
    """)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY api_isolation ON {table} TO recruitment_api
            USING (tenant_id = tenant_context() AND tenant_is_member(tenant_id))
            WITH CHECK (tenant_id = tenant_context() AND tenant_is_member(tenant_id))
        """)
        op.execute(f"""
            CREATE POLICY worker_isolation ON {table} TO recruitment_worker
            USING (tenant_id = tenant_context()) WITH CHECK (tenant_id = tenant_context())
        """)
        op.execute(f"GRANT SELECT ON {table} TO recruitment_api, recruitment_worker")
    for table in (
        "job_openings",
        "job_criteria_versions",
        "job_criteria",
        "resume_files",
        "cv_uploads",
        "review_decisions",
    ):
        op.execute(f"GRANT INSERT ON {table} TO recruitment_api")
    op.execute("GRANT UPDATE ON applications TO recruitment_api")
    op.execute("GRANT UPDATE (retention_expires_at) ON cv_uploads TO recruitment_api")
    op.execute("GRANT UPDATE (embedding, embedding_model) ON job_criteria TO recruitment_worker")
    for table in TABLES:
        if table not in {
            "job_openings",
            "job_criteria_versions",
            "job_criteria",
            "resume_files",
            "review_decisions",
        }:
            op.execute(f"GRANT INSERT, UPDATE ON {table} TO recruitment_worker")
        if table not in {"job_openings", "job_criteria_versions", "job_criteria"}:
            op.execute(f"GRANT DELETE ON {table} TO recruitment_worker")


def downgrade() -> None:
    op.execute("ALTER TABLE applications DROP CONSTRAINT fk_latest_match")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table} CASCADE")
    op.execute("DROP FUNCTION protect_rejection()")
    op.execute("DROP FUNCTION immutable_criteria()")
    # Keep the extension: another schema may also depend on it.
