"""Replace vector scoring with one immutable match scope per incoming CV."""

from alembic import op

revision = "0015_cv_json_matching"
down_revision = "0014_full_text_scoring"
branch_labels = None
depends_on = None

DISPATCH = """
CREATE OR REPLACE FUNCTION public.dispatch_outbox() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE item record; dispatched integer := 0;
BEGIN
    FOR item IN SELECT * FROM public.outbox_messages
        WHERE dispatched_at IS NULL AND available_at <= now()
        ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 100
    LOOP
        IF item.kind IN ('cv.uploaded','retention.settings_changed','retention.application_due',
            'retention.upload_due','mailbox.sync','mailbox.maintain','mailbox.disconnect',
            'mail.attachment','retention.mail_due') THEN
            INSERT INTO public.job_runs
                (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,state,attempts,available_at)
            VALUES(gen_random_uuid(),now(),item.tenant_id,item.kind,item.aggregate_id,
                item.dedupe_key,'queued',0,now())
            ON CONFLICT(dedupe_key) DO NOTHING;
        END IF;
        UPDATE public.outbox_messages SET dispatched_at=now() WHERE id=item.id;
        dispatched := dispatched+1;
    END LOOP;
    RETURN dispatched;
END $$;
"""


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conrelid='candidate_contacts'::regclass
                    AND pg_get_constraintdef(oid)='UNIQUE (tenant_id, candidate_id, kind)'
            ) THEN
                ALTER TABLE candidate_contacts ADD UNIQUE (tenant_id, candidate_id, kind);
            END IF;
        END $$;
        ALTER TABLE opening_intake_periods ADD COLUMN match_description json;
        UPDATE opening_intake_periods p SET match_description=json_build_object(
            'title',o.title,'description',o.job_description,
            'requirements',COALESCE(o.details->>'candidate_requirements',''),
            'min_experience_years',COALESCE(o.details->'total_experience_years','0'::json),
            'relevant_experience_years',o.details->'relevant_experience_years',
            'domain_experience',COALESCE(o.details->>'domain_experience',''))
        FROM job_openings o WHERE o.tenant_id=p.tenant_id AND o.id=p.opening_id;
        UPDATE opening_intake_periods
        SET match_description=jsonb_set(match_description::jsonb,'{min_experience_years}','0')::json
        WHERE match_description->>'min_experience_years' IS NULL;
        ALTER TABLE opening_intake_periods ALTER COLUMN match_description SET NOT NULL;

        ALTER TABLE resume_files DROP CONSTRAINT resume_files_supported_mime,
            ADD CONSTRAINT resume_files_supported_mime CHECK (mime_type IN (
                'application/pdf',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'application/msword','image/jpeg','image/png'));
        ALTER TABLE cv_uploads ADD COLUMN matching_openings json NOT NULL DEFAULT '[]';
        ALTER TABLE role_assessments ADD COLUMN match_result json;

        -- Resume uncompleted intake. Existing results stay attached to their original CVs.
        UPDATE cv_uploads u SET matching_openings=COALESCE((
            SELECT json_agg(json_build_object('opening_id',p.opening_id,'jd',p.match_description)
                            ORDER BY p.opening_id)
            FROM opening_intake_periods p
            WHERE p.tenant_id=u.tenant_id AND p.starts_at<=u.source_received_at
                AND (p.ends_at IS NULL OR p.ends_at>u.source_received_at)
        ),'[]'::json)
        WHERE u.status IN ('queued','extracting','profiling','embedding','matching','retry_wait',
                           'needs_review');
        UPDATE cv_uploads SET status='queued',review_reason=NULL
        WHERE status IN ('extracting','profiling','embedding','matching','retry_wait')
            OR (status='needs_review' AND review_reason='JOB_REQUIREMENTS_FAILED'
                AND application_id IS NULL);
        ALTER TABLE cv_uploads DROP CONSTRAINT cv_uploads_status_check,
            ADD CONSTRAINT cv_uploads_status_check CHECK (status IN
                ('queued','extracting','profiling','matching','retry_wait','completed',
                 'needs_review'));
        UPDATE job_runs j SET state='queued',attempts=0,lease_token=NULL,lease_expires_at=NULL,
                available_at=now(),failure_code=NULL
        FROM cv_uploads u
        WHERE j.kind='cv.uploaded' AND j.state IN ('queued','running','retry_wait')
            AND (u.tenant_id,u.id)=(j.tenant_id,j.aggregate_id) AND u.status='queued';
        -- Obsolete reevaluations must not rescore completed or reviewed profiles.
        UPDATE job_runs j SET state='succeeded',lease_token=NULL,lease_expires_at=NULL
        WHERE j.state IN ('queued','running','retry_wait') AND
            (j.kind='opening.prepare' OR (j.kind='cv.uploaded' AND NOT EXISTS (
                SELECT 1 FROM cv_uploads u WHERE (u.tenant_id,u.id)=(j.tenant_id,j.aggregate_id)
                    AND u.status='queued' AND u.retention_expires_at>now()
            )));
        UPDATE outbox_messages e SET dispatched_at=now()
        WHERE e.dispatched_at IS NULL AND
            (e.kind='opening.prepare' OR (e.kind='cv.uploaded' AND NOT EXISTS (
                SELECT 1 FROM cv_uploads u WHERE (u.tenant_id,u.id)=(e.tenant_id,e.aggregate_id)
                    AND u.status='queued' AND u.retention_expires_at>now()
            )));
        INSERT INTO outbox_messages(id,created_at,tenant_id,kind,aggregate_id,dedupe_key)
        SELECT gen_random_uuid(),now(),u.tenant_id,'cv.uploaded',u.id,'cv.cutover:'||u.id
        FROM cv_uploads u WHERE u.status='queued' AND u.retention_expires_at>now()
            AND NOT EXISTS (SELECT 1 FROM job_runs j WHERE j.kind='cv.uploaded'
                AND (j.tenant_id,j.aggregate_id)=(u.tenant_id,u.id) AND j.state='queued')
            AND NOT EXISTS (SELECT 1 FROM outbox_messages e WHERE e.kind='cv.uploaded'
                AND (e.tenant_id,e.aggregate_id)=(u.tenant_id,u.id) AND e.dispatched_at IS NULL)
        ON CONFLICT (dedupe_key) DO NOTHING;

        DROP TABLE cv_embedding_batches, resume_chunks, cv_processing_snapshots;
        ALTER TABLE job_criteria DROP COLUMN embedding,DROP COLUMN embedding_model,
            DROP COLUMN embedding_fingerprint;
        ALTER TABLE match_runs DROP CONSTRAINT match_run_embedding_strategy,
            DROP COLUMN embedding_model,DROP COLUMN embedding_dimensions,
            DROP COLUMN embedding_fingerprint,DROP COLUMN processing_config,
            DROP COLUMN scoring_strategy;
        ALTER TABLE criterion_results DROP COLUMN evidence;
        ALTER TABLE outbox_messages DROP COLUMN scoring_strategy;
        ALTER TABLE job_runs DROP COLUMN scoring_strategy;
        ALTER TABLE role_assessments DROP COLUMN grade_checkpoints,DROP COLUMN eligibility;
        ALTER TABLE matching_evaluations DROP COLUMN experience_facts;
        UPDATE matching_evaluations SET config=json_build_object('pipeline','legacy');
        ALTER TABLE job_openings DROP COLUMN criteria_status,DROP COLUMN criteria_error,
            DROP COLUMN preparation_config;
        ALTER TABLE opening_documents DROP COLUMN extracted_pages,DROP COLUMN extraction_method;
        REVOKE UPDATE (status,review_reason) ON cv_uploads FROM recruitment_api;
        REVOKE INSERT,UPDATE ON job_criteria,job_criteria_versions FROM recruitment_worker;
        REVOKE INSERT ON job_criteria,job_criteria_versions FROM recruitment_api;
        DROP EXTENSION IF EXISTS vector RESTRICT;
    """)
    op.execute(DISPATCH)


def downgrade() -> None:
    raise RuntimeError("Restore the pre-cutover backup to roll back revision 0015.")
