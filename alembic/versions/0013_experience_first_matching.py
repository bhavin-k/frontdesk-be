"""Experience gates, multi-role evaluations and recruiter-controlled outcomes."""

from alembic import op

revision = "0013_experience_first_matching"
down_revision = "0012_opening_setup_lifecycle"
branch_labels = None
depends_on = None


def decision_guard(enabled: bool) -> None:
    immutable = (
        "OR NEW.latest_evaluation_id IS DISTINCT FROM OLD.latest_evaluation_id" if enabled else ""
    )
    reference = "match_id = NEW.latest_match_id"
    if enabled:
        reference = """((NEW.latest_evaluation_id IS NULL AND match_id = NEW.latest_match_id)
            OR (evaluation_id = NEW.latest_evaluation_id AND EXISTS(
                SELECT 1 FROM public.matching_evaluations e WHERE e.id=evaluation_id
                AND e.application_id=NEW.id AND e.resume_id=NEW.current_resume_id
                AND e.state='completed')))"""
    selected = ""
    if enabled:
        selected = """
            IF NEW.status='shortlisted' AND NEW.latest_evaluation_id IS NOT NULL AND
                (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status
                 OR OLD.opening_id IS DISTINCT FROM NEW.opening_id) THEN
                IF current_user <> 'recruitment_api'
                    OR NOT public.tenant_is_member(NEW.tenant_id) OR NOT EXISTS(
                        SELECT 1 FROM public.review_decisions d
                        JOIN public.role_assessments r ON r.id=d.role_assessment_id
                            AND r.evaluation_id=d.evaluation_id AND r.tenant_id=d.tenant_id
                        WHERE d.tenant_id=NEW.tenant_id AND d.application_id=NEW.id
                        AND d.evaluation_id=NEW.latest_evaluation_id
                        AND r.opening_id=NEW.opening_id AND d.decision='shortlist'
                        AND d.recruiter_id=public.actor_context()
                        AND d.created_at >= transaction_timestamp()
                    ) THEN
                    RAISE EXCEPTION 'Explicit recruiter selection required' USING ERRCODE='42501';
                END IF;
            END IF;
        """
    op.execute(f"""
        CREATE OR REPLACE FUNCTION protect_rejection() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$ BEGIN
            IF TG_OP='UPDATE' AND OLD.status='rejected'
                AND current_user IN ('recruitment_api','recruitment_worker') AND (
                    NEW.status IS DISTINCT FROM OLD.status
                    OR NEW.rejected_at IS DISTINCT FROM OLD.rejected_at
                    OR NEW.latest_match_id IS DISTINCT FROM OLD.latest_match_id {immutable}) THEN
                RAISE EXCEPTION 'Confirmed rejection is immutable' USING ERRCODE='42501';
            END IF;
            IF NEW.status='rejected' AND (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status)
            THEN
                IF current_user <> 'recruitment_api' OR NOT public.tenant_is_member(NEW.tenant_id)
                    OR NOT EXISTS(SELECT 1 FROM public.review_decisions
                        WHERE tenant_id=NEW.tenant_id AND application_id=NEW.id AND {reference}
                        AND recruiter_id=public.actor_context() AND decision='reject'
                        AND created_at >= transaction_timestamp()) THEN
                    RAISE EXCEPTION 'Explicit recruiter decision required' USING ERRCODE='42501';
                END IF;
            END IF;
            {selected}
            RETURN NEW;
        END $$;
    """)


def upgrade() -> None:
    op.execute("""
        ALTER TABLE match_runs ADD COLUMN policy_version text NOT NULL DEFAULT 'legacy-v1';
        ALTER TABLE match_runs DROP CONSTRAINT match_runs_resume_id_criteria_version_id_key;
        ALTER TABLE match_runs ADD CONSTRAINT uq_match_policy
            UNIQUE(resume_id,criteria_version_id,policy_version);
        CREATE TABLE matching_evaluations (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), application_id uuid NOT NULL,
            resume_id uuid NOT NULL, signature text NOT NULL, role_snapshot json NOT NULL,
            config json NOT NULL, state text NOT NULL
                CHECK(state IN ('processing','completed','superseded')),
            recommendation text NOT NULL CHECK(recommendation IN
                ('recommended_shortlist','recommended_rejection','needs_review')),
            reason text NOT NULL, confidence numeric(5,4) CHECK(confidence BETWEEN 0 AND 1),
            recommended_assessment_id uuid, experience_facts json, processed_at timestamptz,
            UNIQUE(tenant_id,id), UNIQUE(tenant_id,application_id,id), UNIQUE(resume_id,signature),
            FOREIGN KEY(tenant_id,application_id)
                REFERENCES applications(tenant_id,id) ON DELETE CASCADE,
            FOREIGN KEY(tenant_id,application_id,resume_id)
                REFERENCES resumes(tenant_id,application_id,id) ON DELETE CASCADE
        );
        CREATE TABLE role_assessments (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), evaluation_id uuid NOT NULL,
            application_id uuid NOT NULL,
            opening_id uuid NOT NULL, criteria_version_id uuid,
            state text NOT NULL CHECK(state IN ('pending','processing','completed','failed')),
            eligibility json NOT NULL DEFAULT '{}',
            recommendation text NOT NULL CHECK(recommendation IN
                ('recommended_shortlist','recommended_rejection','needs_review')),
            confidence numeric(5,4) CHECK(confidence BETWEEN 0 AND 1),
            score numeric(5,2) CHECK(score BETWEEN 0 AND 100),
            provisional boolean NOT NULL DEFAULT false, reason text NOT NULL,
            match_run_id uuid, grade_checkpoints json NOT NULL DEFAULT '{}', failure_code text,
            UNIQUE(tenant_id,id), UNIQUE(tenant_id,evaluation_id,id),
            UNIQUE(evaluation_id,opening_id),
            FOREIGN KEY(tenant_id,application_id,evaluation_id)
                REFERENCES matching_evaluations(tenant_id,application_id,id) ON DELETE CASCADE,
            FOREIGN KEY(tenant_id,opening_id) REFERENCES job_openings(tenant_id,id),
            FOREIGN KEY(tenant_id,criteria_version_id)
                REFERENCES job_criteria_versions(tenant_id,id),
            CONSTRAINT fk_assessment_match FOREIGN KEY(tenant_id,application_id,match_run_id)
                REFERENCES match_runs(tenant_id,application_id,id) ON DELETE CASCADE
        );
        CREATE INDEX ix_role_assessment_evaluation ON role_assessments(tenant_id,evaluation_id);
        ALTER TABLE matching_evaluations ADD CONSTRAINT fk_recommended_assessment
            FOREIGN KEY(tenant_id,id,recommended_assessment_id)
            REFERENCES role_assessments(tenant_id,evaluation_id,id) DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE applications ADD COLUMN latest_evaluation_id uuid;
        ALTER TABLE applications ADD CONSTRAINT fk_latest_evaluation
            FOREIGN KEY(tenant_id,id,latest_evaluation_id)
            REFERENCES matching_evaluations(tenant_id,application_id,id)
                DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE review_decisions ADD COLUMN evaluation_id uuid,
            ADD COLUMN role_assessment_id uuid,
            ALTER COLUMN match_id DROP NOT NULL, ALTER COLUMN reason DROP NOT NULL;
        ALTER TABLE review_decisions ADD FOREIGN KEY(tenant_id,application_id,evaluation_id)
            REFERENCES matching_evaluations(tenant_id,application_id,id) ON DELETE CASCADE;
        ALTER TABLE review_decisions ADD FOREIGN KEY(tenant_id,evaluation_id,role_assessment_id)
            REFERENCES role_assessments(tenant_id,evaluation_id,id);
        ALTER TABLE review_decisions ADD CONSTRAINT ck_decision_reference CHECK
            (match_id IS NOT NULL OR evaluation_id IS NOT NULL);
        ALTER TABLE review_decisions ADD CONSTRAINT ck_decision_assessment CHECK
            (role_assessment_id IS NULL OR evaluation_id IS NOT NULL);
        GRANT UPDATE ON applications TO recruitment_api,recruitment_worker;
    """)
    for table in ("matching_evaluations", "role_assessments"):
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY api_isolation ON {table} TO recruitment_api
                USING(tenant_id=tenant_context() AND tenant_is_member(tenant_id));
            CREATE POLICY worker_isolation ON {table} TO recruitment_worker
                USING(tenant_id=tenant_context()) WITH CHECK(tenant_id=tenant_context());
            GRANT SELECT ON {table} TO recruitment_api;
            GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO recruitment_worker;
        """)
    decision_guard(True)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM matching_evaluations)
                OR EXISTS(SELECT 1 FROM review_decisions
                    WHERE reason IS NULL OR evaluation_id IS NOT NULL)
                OR EXISTS(SELECT 1 FROM match_runs WHERE policy_version <> 'legacy-v1')
            THEN RAISE EXCEPTION 'Preserve experience evaluations and decisions before downgrade';
            END IF;
        END $$;
    """)
    decision_guard(False)
    op.execute("""
        ALTER TABLE review_decisions DROP CONSTRAINT ck_decision_reference,
            DROP CONSTRAINT ck_decision_assessment, DROP COLUMN role_assessment_id,
            DROP COLUMN evaluation_id, ALTER COLUMN match_id SET NOT NULL,
            ALTER COLUMN reason SET NOT NULL;
        ALTER TABLE applications DROP COLUMN latest_evaluation_id;
        ALTER TABLE matching_evaluations DROP CONSTRAINT fk_recommended_assessment;
        DROP TABLE role_assessments,matching_evaluations;
        ALTER TABLE match_runs DROP CONSTRAINT uq_match_policy, DROP COLUMN policy_version;
        ALTER TABLE match_runs ADD UNIQUE(resume_id,criteria_version_id);
    """)
