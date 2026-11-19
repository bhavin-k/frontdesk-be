"""Add tenant-scoped email screening and isolate pilot worker jobs."""

from alembic import op

revision = "0016_email_screening"
down_revision = "0015_cv_json_matching"
branch_labels = None
depends_on = None

TABLES = (
    "screening_requests",
    "screening_threads",
    "screening_messages",
    "screening_slots",
    "screening_settings",
    "screening_inboxes",
    "screening_events",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE screening_requests (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            application_id uuid NOT NULL, resume_id uuid NOT NULL, opening_id uuid NOT NULL,
            candidate_name text NOT NULL, candidate_email text NOT NULL,
            callback_number text NOT NULL, role_title text NOT NULL, candidate_timezone text,
            status text NOT NULL CHECK (status IN (
                'SHORTLISTED','EMAIL_SENT','AWAITING_REPLY','NEGOTIATING','BOOKED',
                'NO_RESPONSE','DECLINED','CANCELLED','EMAIL_MISSING','NEEDS_HUMAN',
                'FAILED_DELIVERY','CLOSED'
            )),
            reason text NOT NULL, generation integer NOT NULL CHECK (generation >= 1),
            lock_version integer NOT NULL CHECK (lock_version >= 1),
            auto_replies integer NOT NULL CHECK (auto_replies BETWEEN 0 AND 8),
            takeover boolean NOT NULL, first_sent_at timestamptz, expires_at timestamptz,
            replied_at timestamptz, pending_slot_id uuid,
            UNIQUE (tenant_id,id), UNIQUE (tenant_id,resume_id,opening_id),
            FOREIGN KEY (tenant_id,application_id,resume_id)
                REFERENCES resumes(tenant_id,application_id,id) ON DELETE CASCADE,
            FOREIGN KEY (tenant_id,opening_id) REFERENCES job_openings(tenant_id,id)
        );
        CREATE INDEX ix_screening_queue ON screening_requests(tenant_id,created_at DESC,id);
        CREATE INDEX ix_screening_application ON screening_requests(tenant_id,application_id);
        CREATE TABLE screening_threads (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            request_id uuid NOT NULL, mailbox text NOT NULL, subject text NOT NULL,
            UNIQUE (tenant_id,id), UNIQUE (tenant_id,request_id),
            UNIQUE (tenant_id,id,mailbox),
            FOREIGN KEY (tenant_id,request_id)
                REFERENCES screening_requests(tenant_id,id) ON DELETE CASCADE
        );
        CREATE TABLE screening_messages (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            thread_id uuid NOT NULL, mailbox text NOT NULL, message_id text NOT NULL,
            in_reply_to text, reply_references json NOT NULL, offered_slot_ids json NOT NULL,
            generation integer NOT NULL CHECK (generation >= 1),
            direction text NOT NULL CHECK (direction IN ('in','out')),
            kind text NOT NULL,
            state text NOT NULL CHECK (state IN (
                'queued','sending','sent','received','handled','failed','uncertain','skipped'
            )),
            body text NOT NULL, intent text, sent_at timestamptz, last_attempt_at timestamptz,
            UNIQUE (tenant_id,mailbox,message_id),
            FOREIGN KEY (tenant_id,thread_id,mailbox)
                REFERENCES screening_threads(tenant_id,id,mailbox) ON DELETE CASCADE
        );
        CREATE INDEX ix_screening_thread_messages
            ON screening_messages(tenant_id,thread_id,created_at,id);
        CREATE INDEX ix_screening_send_rate ON screening_messages(tenant_id,last_attempt_at)
            WHERE direction='out' AND last_attempt_at IS NOT NULL;
        CREATE TABLE screening_slots (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), request_id uuid NOT NULL,
            starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL,
            blocked_until timestamptz NOT NULL, hold_until timestamptz,
            status text NOT NULL CHECK (status IN ('held','booked','released')),
            CHECK (starts_at < ends_at AND ends_at <= blocked_until),
            CHECK (status <> 'held' OR hold_until IS NOT NULL),
            FOREIGN KEY (tenant_id,request_id)
                REFERENCES screening_requests(tenant_id,id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX uq_screening_occupied_start ON screening_slots(tenant_id,starts_at)
            WHERE status IN ('held','booked');
        CREATE UNIQUE INDEX uq_screening_request_booking ON screening_slots(tenant_id,request_id)
            WHERE status='booked';
        CREATE INDEX ix_screening_request_slots ON screening_slots(tenant_id,request_id);
        CREATE TABLE screening_settings (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL UNIQUE REFERENCES tenants(id),
            invitation_note text NOT NULL, signature text NOT NULL
        );
        CREATE TABLE screening_inboxes (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            mailbox text NOT NULL, cursor json NOT NULL,
            UNIQUE (tenant_id,mailbox)
        );
        CREATE TABLE screening_events (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), request_id uuid NOT NULL,
            status text NOT NULL, actor text NOT NULL CHECK (actor IN ('agent','tenant','system')),
            actor_id uuid, reason text NOT NULL,
            FOREIGN KEY (tenant_id,request_id)
                REFERENCES screening_requests(tenant_id,id) ON DELETE CASCADE
        );
        CREATE INDEX ix_screening_events ON screening_events(tenant_id,request_id,created_at);
    """)
    for table in TABLES:
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY api_isolation ON {table} TO recruitment_api
                USING (tenant_id=tenant_context() AND tenant_is_member(tenant_id))
                WITH CHECK (tenant_id=tenant_context() AND tenant_is_member(tenant_id));
            CREATE POLICY worker_isolation ON {table} TO recruitment_worker
                USING (tenant_id=tenant_context()) WITH CHECK (tenant_id=tenant_context());
            GRANT SELECT, INSERT, UPDATE ON {table} TO recruitment_api;
            GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO recruitment_worker;
        """)
    op.execute("""
        REVOKE INSERT, UPDATE ON screening_inboxes FROM recruitment_api;

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
                    'mailbox.maintain','mailbox.disconnect',
                    'mail.attachment','retention.mail_due','screening.send','screening.reply',
                    'screening.expire','screening.poll') THEN
                    INSERT INTO public.job_runs
                        (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,state,attempts,available_at)
                    VALUES(gen_random_uuid(),now(),item.tenant_id,item.kind,item.aggregate_id,
                        item.dedupe_key,'queued',0,item.available_at)
                    ON CONFLICT(dedupe_key) DO NOTHING;
                END IF;
                UPDATE public.outbox_messages SET dispatched_at=now() WHERE id=item.id;
                dispatched := dispatched+1;
            END LOOP;
            RETURN dispatched;
        END $$;

        CREATE FUNCTION public.claim_pilot_job(lease_seconds integer, pilot uuid)
        RETURNS TABLE(id uuid, tenant_id uuid, kind text, aggregate_id uuid,
                      lease_token uuid, attempts integer)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE claimed uuid;
        BEGIN
            IF lease_seconds < 1 OR lease_seconds > 3600 THEN
                RAISE EXCEPTION 'Invalid lease' USING ERRCODE='22023';
            END IF;
            SELECT j.id INTO claimed FROM public.job_runs j
                WHERE ((j.state IN ('queued','retry_wait') AND j.available_at <= now())
                    OR (j.state='running' AND j.lease_expires_at <= now()))
                    AND (j.kind NOT LIKE 'screening.%' OR j.tenant_id=pilot)
                ORDER BY j.available_at,j.id FOR UPDATE SKIP LOCKED LIMIT 1;
            RETURN QUERY UPDATE public.job_runs j SET state='running',
                attempts=j.attempts+1, lease_token=gen_random_uuid(),
                lease_expires_at=now()+make_interval(secs => lease_seconds)
                WHERE j.id=claimed
                RETURNING j.id,j.tenant_id,j.kind,j.aggregate_id,j.lease_token,j.attempts;
        END $$;
        REVOKE ALL ON FUNCTION public.claim_pilot_job(integer,uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.claim_pilot_job(integer,uuid) TO recruitment_dispatcher;
        CREATE OR REPLACE FUNCTION public.claim_job(lease_seconds integer)
        RETURNS TABLE(id uuid, tenant_id uuid, kind text, aggregate_id uuid,
                      lease_token uuid, attempts integer)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
            SELECT * FROM public.claim_pilot_job(lease_seconds,NULL::uuid)
        $$;

        CREATE FUNCTION public.schedule_screening_poll(pilot uuid)
        RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
            INSERT INTO public.outbox_messages(
                id,created_at,tenant_id,kind,aggregate_id,available_at,dedupe_key
            )
            SELECT gen_random_uuid(),now(),pilot,'screening.poll',pilot,now(),
                'screening.poll:'||pilot||':'||floor(extract(epoch FROM now())/60)::bigint
            WHERE EXISTS (SELECT 1 FROM public.screening_threads WHERE tenant_id=pilot)
            ON CONFLICT(dedupe_key) DO NOTHING
        $$;
        REVOKE ALL ON FUNCTION public.schedule_screening_poll(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.schedule_screening_poll(uuid) TO recruitment_dispatcher;
    """)


def downgrade() -> None:
    raise RuntimeError("Restore the pre-pilot backup to roll back revision 0016.")
