"""Persist the scoring strategy alongside queued work and match evidence."""

from alembic import op

revision = "0014_full_text_scoring"
down_revision = "0013a_recruiter_decision_guard"
branch_labels = None
depends_on = None

STRATEGY_TABLES = ("outbox_messages", "job_runs", "cv_processing_snapshots", "match_runs")


def _dispatcher(strategy: bool) -> None:
    column = ",scoring_strategy" if strategy else ""
    value = ",item.scoring_strategy" if strategy else ""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.dispatch_outbox() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE item record; dispatched integer := 0;
        BEGIN
            FOR item IN SELECT * FROM public.outbox_messages
                WHERE dispatched_at IS NULL AND available_at <= now()
                ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 100
            LOOP
                IF item.kind IN ('cv.uploaded','retention.settings_changed',
                    'retention.application_due','retention.upload_due','mailbox.sync',
                    'mailbox.maintain','mailbox.disconnect','mail.attachment','retention.mail_due',
                    'opening.prepare') THEN
                    INSERT INTO public.job_runs
                        (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,state,attempts,
                         available_at{column})
                    VALUES(gen_random_uuid(),now(),item.tenant_id,item.kind,item.aggregate_id,
                        item.dedupe_key,'queued',0,now(){value})
                    ON CONFLICT(dedupe_key) DO NOTHING;
                END IF;
                UPDATE public.outbox_messages SET dispatched_at=now() WHERE id=item.id;
                dispatched := dispatched+1;
            END LOOP;
            RETURN dispatched;
        END $$;
    """)


def upgrade() -> None:
    for table in STRATEGY_TABLES:
        op.execute(f"""
            ALTER TABLE {table} ADD COLUMN scoring_strategy text NOT NULL DEFAULT 'rag'
                CHECK(scoring_strategy IN ('rag','full_text'));
        """)
    op.execute("""
        ALTER TABLE cv_processing_snapshots
            DROP CONSTRAINT cv_processing_snapshots_upload_id_key,
            ADD CONSTRAINT uq_cv_snapshot_strategy UNIQUE(upload_id,scoring_strategy);
        ALTER TABLE match_runs
            ALTER COLUMN embedding_model DROP NOT NULL,
            ALTER COLUMN embedding_dimensions DROP NOT NULL,
            ALTER COLUMN embedding_fingerprint DROP NOT NULL,
            ALTER COLUMN embedding_fingerprint DROP DEFAULT,
            ADD CONSTRAINT match_run_embedding_strategy CHECK (
                (scoring_strategy='rag' AND embedding_model IS NOT NULL
                    AND embedding_dimensions IS NOT NULL AND embedding_fingerprint IS NOT NULL)
                OR (scoring_strategy='full_text' AND embedding_model IS NULL
                    AND embedding_dimensions IS NULL AND embedding_fingerprint IS NULL)
            );
    """)
    _dispatcher(strategy=True)


def downgrade() -> None:
    checks = " OR ".join(
        f"EXISTS(SELECT 1 FROM {table} WHERE scoring_strategy='full_text')"
        for table in STRATEGY_TABLES
    )
    op.execute(f"""
        DO $$ BEGIN
            IF {checks} THEN
                RAISE EXCEPTION 'Cannot downgrade with full-text scoring data';
            END IF;
        END $$;
        ALTER TABLE cv_processing_snapshots DROP CONSTRAINT uq_cv_snapshot_strategy,
            ADD CONSTRAINT cv_processing_snapshots_upload_id_key UNIQUE(upload_id);
        ALTER TABLE match_runs DROP CONSTRAINT match_run_embedding_strategy,
            ALTER COLUMN embedding_model SET NOT NULL,
            ALTER COLUMN embedding_dimensions SET NOT NULL,
            ALTER COLUMN embedding_fingerprint SET NOT NULL,
            ALTER COLUMN embedding_fingerprint SET DEFAULT 'legacy-unfingerprinted';
    """)
    _dispatcher(strategy=False)
    for table in STRATEGY_TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN scoring_strategy")
