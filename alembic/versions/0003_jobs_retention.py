"""Transactional outbox, fenced job leases and candidate retention tombstones."""

from alembic import op

revision = "0003_jobs_retention"
down_revision = "0002_cv_matching"
branch_labels = None
depends_on = None

TABLES = (
    "outbox_messages",
    "job_runs",
    "failed_jobs",
    "idempotency_records",
    "candidate_data_tombstones",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE outbox_messages (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), kind text NOT NULL,
            aggregate_id uuid NOT NULL, dedupe_key text NOT NULL UNIQUE,
            available_at timestamptz NOT NULL, dispatched_at timestamptz
        );
        CREATE INDEX ix_outbox_pending ON outbox_messages(available_at)
            WHERE dispatched_at IS NULL;
        CREATE TABLE job_runs (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), kind text NOT NULL,
            aggregate_id uuid NOT NULL, dedupe_key text NOT NULL UNIQUE,
            state text NOT NULL CHECK(state IN
                ('queued','running','retry_wait','succeeded','failed')),
            attempts integer NOT NULL CHECK(attempts >= 0), available_at timestamptz NOT NULL,
            lease_token uuid, lease_expires_at timestamptz, failure_code text,
            UNIQUE(tenant_id, id),
            CHECK((state = 'running') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))
        );
        CREATE INDEX ix_job_claim ON job_runs(available_at, lease_expires_at)
            WHERE state IN ('queued','retry_wait','running');
        CREATE TABLE failed_jobs (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), job_id uuid NOT NULL,
            aggregate_id uuid NOT NULL, failure_code text NOT NULL
        );
        CREATE TABLE idempotency_records (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), scope text NOT NULL,
            key varchar(128) NOT NULL, fingerprint varchar(64) NOT NULL, resource_id uuid NOT NULL,
            UNIQUE(tenant_id, scope, key)
        );
        CREATE TABLE candidate_data_tombstones (
            tenant_id uuid NOT NULL REFERENCES tenants(id), application_id uuid NOT NULL,
            role_id uuid NOT NULL, final_status text NOT NULL, deleted_at timestamptz NOT NULL,
            PRIMARY KEY(tenant_id, application_id)
        );
        CREATE FUNCTION dispatch_outbox() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE item record; dispatched integer := 0;
        BEGIN
            FOR item IN SELECT * FROM public.outbox_messages
                WHERE dispatched_at IS NULL AND available_at <= now()
                ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 100
            LOOP
                IF item.kind IN ('cv.uploaded','retention.settings_changed',
                                 'retention.application_due','retention.upload_due') THEN
                    INSERT INTO public.job_runs
                        (id, created_at, tenant_id, kind, aggregate_id, dedupe_key,
                         state, attempts, available_at)
                    VALUES (gen_random_uuid(), now(), item.tenant_id, item.kind, item.aggregate_id,
                        item.dedupe_key, 'queued', 0, now())
                    ON CONFLICT(dedupe_key) DO NOTHING;
                END IF;
                UPDATE public.outbox_messages SET dispatched_at = now() WHERE id = item.id;
                dispatched := dispatched + 1;
            END LOOP;
            RETURN dispatched;
        END $$;
        CREATE FUNCTION claim_job(lease_seconds integer)
            RETURNS TABLE(id uuid, tenant_id uuid, kind text, aggregate_id uuid,
                          lease_token uuid, attempts integer)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE claimed uuid;
        BEGIN
            IF lease_seconds < 1 OR lease_seconds > 3600 THEN
                RAISE EXCEPTION 'Invalid lease' USING ERRCODE = '22023';
            END IF;
            SELECT j.id INTO claimed FROM public.job_runs j
                WHERE ((j.state IN ('queued','retry_wait') AND j.available_at <= now())
                    OR (j.state = 'running' AND j.lease_expires_at <= now()))
                ORDER BY j.available_at, j.id FOR UPDATE SKIP LOCKED LIMIT 1;
            RETURN QUERY UPDATE public.job_runs j SET state = 'running',
                attempts = j.attempts + 1, lease_token = gen_random_uuid(),
                lease_expires_at = now() + make_interval(secs => lease_seconds)
                WHERE j.id = claimed
                RETURNING j.id, j.tenant_id, j.kind, j.aggregate_id, j.lease_token, j.attempts;
        END $$;
        CREATE FUNCTION schedule_retention() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE affected integer;
        BEGIN
            INSERT INTO public.outbox_messages
                (id, created_at, tenant_id, kind, aggregate_id, dedupe_key, available_at)
            SELECT gen_random_uuid(), now(), a.tenant_id, 'retention.application_due',
                a.id, 'expiry:application:' || a.id || ':' || a.retention_expires_at, now()
                FROM public.applications a WHERE a.retention_expires_at <= now()
            UNION ALL
            SELECT gen_random_uuid(), now(), u.tenant_id, 'retention.upload_due',
                u.id, 'expiry:upload:' || u.id || ':' || u.retention_expires_at, now()
                FROM public.cv_uploads u
                WHERE u.application_id IS NULL AND u.retention_expires_at <= now()
            ON CONFLICT(dedupe_key) DO NOTHING;
            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected;
        END $$;
        REVOKE ALL ON FUNCTION dispatch_outbox(), claim_job(integer), schedule_retention() FROM
            PUBLIC;
        GRANT EXECUTE ON FUNCTION dispatch_outbox(), claim_job(integer), schedule_retention()
            TO recruitment_dispatcher;
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
        op.execute(f"GRANT INSERT, UPDATE, DELETE ON {table} TO recruitment_worker")
    op.execute("GRANT INSERT ON outbox_messages, idempotency_records TO recruitment_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION schedule_retention()")
    op.execute("DROP FUNCTION claim_job(integer)")
    op.execute("DROP FUNCTION dispatch_outbox()")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
