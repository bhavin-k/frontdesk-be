"""Namespaced embeddings, resumable batches and durable mailbox job scheduling."""

from alembic import op

revision = "0009_phase2_jobs_rag"
down_revision = "0008_candidate_contact_review"
branch_labels = None
depends_on = None

TABLES = ("cv_processing_snapshots", "cv_embedding_batches")


def dispatcher(mailboxes: bool) -> None:
    kinds = (
        "'cv.uploaded','retention.settings_changed',"
        "'retention.application_due','retention.upload_due'"
    )
    if mailboxes:
        kinds += (
            ",'mailbox.sync','mailbox.maintain','mailbox.disconnect',"
            "'mail.attachment','retention.mail_due'"
        )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION dispatch_outbox() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
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
        ALTER TABLE resume_chunks ADD COLUMN embedding_fingerprint text NOT NULL
            DEFAULT 'legacy-unfingerprinted';
        ALTER TABLE job_criteria ADD COLUMN embedding_fingerprint text;
        ALTER TABLE match_runs ADD COLUMN embedding_fingerprint text NOT NULL DEFAULT
        'legacy-unfingerprinted',
            ADD COLUMN processing_config json NOT NULL DEFAULT '{}';
        CREATE TABLE cv_processing_snapshots (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), upload_id uuid NOT NULL UNIQUE,
            fingerprint text NOT NULL, config json NOT NULL,
            FOREIGN KEY(tenant_id,upload_id) REFERENCES cv_uploads(tenant_id,id) ON DELETE CASCADE
        );
        CREATE TABLE cv_embedding_batches (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), upload_id uuid NOT NULL,
            position integer NOT NULL CHECK(position >= 0), fingerprint text NOT NULL,
            input_hash text NOT NULL, vectors json NOT NULL, UNIQUE(upload_id,position),
            FOREIGN KEY(tenant_id,upload_id) REFERENCES cv_uploads(tenant_id,id) ON DELETE CASCADE
        );
        GRANT UPDATE (embedding_fingerprint) ON job_criteria TO recruitment_worker;
        CREATE FUNCTION protect_current_match() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog,public AS $$
        BEGIN
            IF NEW.latest_match_id IS DISTINCT FROM OLD.latest_match_id AND NEW.latest_match_id
            IS NOT NULL
                AND NOT EXISTS(SELECT 1 FROM public.match_runs m WHERE m.id=NEW.latest_match_id
                    AND m.tenant_id=NEW.tenant_id AND m.application_id=NEW.id
                    AND m.resume_id=NEW.current_resume_id) THEN
                RAISE EXCEPTION 'Current CV required' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER application_current_match_guard BEFORE UPDATE ON applications
            FOR EACH ROW EXECUTE FUNCTION protect_current_match();
        CREATE FUNCTION schedule_mailboxes() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog,public AS $$
        DECLARE item record; scheduled integer := 0;
        BEGIN
            FOR item IN SELECT * FROM public.mailbox_connections
                WHERE status='connected' AND (next_sync_at<=now() OR next_maintenance_at<=now())
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 100
            LOOP
                IF item.next_sync_at<=now() THEN
                    INSERT INTO public.outbox_messages
                        (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,available_at)
                    VALUES(gen_random_uuid(),now(),item.tenant_id,'mailbox.sync',item.id,
                        'mailbox.sync:'||item.id||':'||item.generation||':'||item.next_sync_at,now())
                        ON CONFLICT(dedupe_key) DO NOTHING;
                    UPDATE public.mailbox_connections SET next_sync_at=now()+interval '15 minutes'
                        WHERE id=item.id;
                END IF;
                IF item.next_maintenance_at<=now() THEN
                    INSERT INTO public.outbox_messages
                        (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,available_at)
                    VALUES(gen_random_uuid(),now(),item.tenant_id,'mailbox.maintain',item.id,
                        'mailbox.maintain:'||item.id||':'||item.generation||':'||item.next_maintenance_at,now())
                        ON CONFLICT(dedupe_key) DO NOTHING;
                    UPDATE public.mailbox_connections SET next_maintenance_at=now()+interval '1
                    hour'
                        WHERE id=item.id;
                END IF;
                scheduled := scheduled+1;
            END LOOP;
            DELETE FROM public.mailbox_oauth_attempts WHERE expires_at < now()-interval '1 day';
            DELETE FROM public.mailbox_notification_receipts WHERE created_at < now()-interval
            '1 day';
            RETURN scheduled;
        END $$;
        REVOKE ALL ON FUNCTION schedule_mailboxes() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION schedule_mailboxes() TO recruitment_dispatcher;
        CREATE FUNCTION schedule_mail_retention() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE affected integer;
        BEGIN
            INSERT INTO public.outbox_messages
                (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,available_at)
            SELECT gen_random_uuid(),now(),m.tenant_id,'retention.mail_due',m.id,
                'expiry:mail:'||m.id||':'||s.other_candidate_retention_days,now()
            FROM public.mail_source_messages m JOIN public.tenant_retention_settings s
                ON s.tenant_id=m.tenant_id
            WHERE m.received_at+make_interval(days=>s.other_candidate_retention_days)<=now()
                AND NOT EXISTS(SELECT 1 FROM public.mail_source_attachments a
                    JOIN public.cv_uploads u ON u.id=a.upload_id WHERE a.message_id=m.id
                        AND u.retention_expires_at>now())
            ON CONFLICT(dedupe_key) DO NOTHING;
            GET DIAGNOSTICS affected=ROW_COUNT;
            RETURN affected;
        END $$;
        REVOKE ALL ON FUNCTION schedule_mail_retention() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION schedule_mail_retention() TO recruitment_dispatcher;
    """)
    dispatcher(True)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY api_isolation ON {table} TO recruitment_api
            USING (tenant_id=tenant_context() AND tenant_is_member(tenant_id))
            WITH CHECK (tenant_id=tenant_context() AND tenant_is_member(tenant_id))""")
        op.execute(f"""CREATE POLICY worker_isolation ON {table} TO recruitment_worker
            USING (tenant_id=tenant_context()) WITH CHECK (tenant_id=tenant_context())""")
        op.execute(f"GRANT SELECT ON {table} TO recruitment_api")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO recruitment_worker")


def downgrade() -> None:
    # Dropping populated processing/mail records would discard an intake in progress.
    op.execute("""
        DO $$ BEGIN IF EXISTS(SELECT 1 FROM cv_processing_snapshots)
            OR EXISTS(SELECT 1 FROM cv_embedding_batches) THEN
            RAISE EXCEPTION 'Preserve processing snapshots and checkpoints before downgrade';
        END IF; END $$;
        DO $$ BEGIN IF EXISTS(SELECT 1 FROM job_runs WHERE kind LIKE 'mail%' AND state IN
            ('queued','running','retry_wait')) THEN RAISE EXCEPTION 'Drain mailbox jobs before
            downgrade';
            END IF; END $$;
        DROP FUNCTION schedule_mailboxes();
        DROP FUNCTION schedule_mail_retention();
        DROP TRIGGER application_current_match_guard ON applications;
        DROP FUNCTION protect_current_match();
        REVOKE UPDATE (embedding_fingerprint) ON job_criteria FROM recruitment_worker;
        ALTER TABLE match_runs DROP COLUMN embedding_fingerprint,DROP COLUMN processing_config;
        ALTER TABLE job_criteria DROP COLUMN embedding_fingerprint;
        ALTER TABLE resume_chunks DROP COLUMN embedding_fingerprint;
    """)
    dispatcher(False)
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
