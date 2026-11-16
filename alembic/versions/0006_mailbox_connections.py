"""Owner mailbox OAuth, encrypted credentials and verified notification routing."""

from alembic import op

revision = "0006_mailbox_connections"
down_revision = "0005_review_retention_permission"
branch_labels = None
depends_on = None

TABLES = (
    "mailbox_connections",
    "mailbox_credentials",
    "mailbox_oauth_attempts",
    "mailbox_notification_receipts",
)


def upgrade() -> None:
    op.execute("""
        -- Earlier local Phase 1 installs used two-column ownership keys. Strengthen
        -- them before current-CV references, without changing the historical baseline.
        DO $$ BEGIN
            IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='resumes'::regclass
                AND contype='u' AND pg_get_constraintdef(oid) =
                    'UNIQUE (tenant_id, application_id, id)') THEN
                ALTER TABLE resumes ADD CONSTRAINT uq_resume_application_scope
                    UNIQUE(tenant_id,application_id,id);
            END IF;
            IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='match_runs'::regclass
                AND contype='u' AND pg_get_constraintdef(oid) =
                    'UNIQUE (tenant_id, application_id, id)') THEN
                ALTER TABLE match_runs ADD CONSTRAINT uq_match_application_scope
                    UNIQUE(tenant_id,application_id,id);
            END IF;
        END $$;
        ALTER TABLE applications DROP CONSTRAINT fk_latest_match;
        ALTER TABLE applications ADD CONSTRAINT fk_latest_match
            FOREIGN KEY(tenant_id,id,latest_match_id)
            REFERENCES match_runs(tenant_id,application_id,id)
            DEFERRABLE INITIALLY DEFERRED;
        DO $$ DECLARE c record; BEGIN
            IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='match_runs'::regclass
                AND contype='f' AND confrelid='resumes'::regclass
                AND pg_get_constraintdef(oid) LIKE
                    'FOREIGN KEY (tenant_id, application_id, resume_id)%') THEN
                FOR c IN SELECT conname FROM pg_constraint
                    WHERE conrelid='match_runs'::regclass AND confrelid='resumes'::regclass
                LOOP EXECUTE format('ALTER TABLE match_runs DROP CONSTRAINT %I',c.conname);
                END LOOP;
                ALTER TABLE match_runs ADD CONSTRAINT fk_match_resume_scope
                    FOREIGN KEY(tenant_id,application_id,resume_id)
                    REFERENCES resumes(tenant_id,application_id,id) ON DELETE CASCADE;
            END IF;
            IF NOT EXISTS(SELECT 1 FROM pg_constraint
                WHERE conrelid='review_decisions'::regclass AND contype='f'
                    AND confrelid='match_runs'::regclass AND pg_get_constraintdef(oid) LIKE
                        'FOREIGN KEY (tenant_id, application_id, match_id)%') THEN
                FOR c IN SELECT conname FROM pg_constraint
                    WHERE conrelid='review_decisions'::regclass
                        AND confrelid='match_runs'::regclass
                LOOP EXECUTE format('ALTER TABLE review_decisions DROP CONSTRAINT %I',c.conname);
                END LOOP;
                ALTER TABLE review_decisions ADD CONSTRAINT fk_review_match_scope
                    FOREIGN KEY(tenant_id,application_id,match_id)
                    REFERENCES match_runs(tenant_id,application_id,id) ON DELETE CASCADE;
            END IF;
        END $$;
        CREATE TABLE mailbox_connections (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), provider text NOT NULL
                CHECK(provider IN ('gmail','microsoft')), recruiter_id uuid NOT NULL REFERENCES
                users(id),
            opening_id uuid NOT NULL, status text NOT NULL CHECK(status IN
                ('pending_authorization','needs_configuration','connected','reauthorization_required',
                 'disconnecting','disconnected','error')),
            email text, owner_hash text, notification_lookup text, folder_id text NOT NULL,
            intake_started_at timestamptz, completed_watermark timestamptz, cursor json NOT NULL,
            generation integer NOT NULL CHECK(generation > 0),
            source_period integer NOT NULL CHECK(source_period > 0), sync_status text NOT NULL
                CHECK(sync_status IN ('idle','queued','running','retry_wait','failed')),
            failure_code text, last_success_at timestamptz, next_sync_at timestamptz,
            next_maintenance_at timestamptz, subscription_id text,
            subscription_expires_at timestamptz, notification_secret_hash text,
            lease_token uuid, lease_expires_at timestamptz, UNIQUE(tenant_id,id),
            FOREIGN KEY(tenant_id,opening_id) REFERENCES job_openings(tenant_id,id),
            CHECK((lease_token IS NULL) = (lease_expires_at IS NULL)),
            CHECK(status <> 'connected' OR intake_started_at IS NOT NULL)
        );
        CREATE UNIQUE INDEX uq_active_mailbox_owner ON mailbox_connections(provider,owner_hash)
            WHERE status NOT IN ('disconnected') AND owner_hash IS NOT NULL;
        CREATE INDEX ix_mailbox_schedule ON mailbox_connections(next_sync_at,next_maintenance_at);
        CREATE TABLE mailbox_credentials (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), connection_id uuid NOT NULL UNIQUE,
            key_id text NOT NULL, nonce bytea NOT NULL, ciphertext bytea NOT NULL,
            FOREIGN KEY(tenant_id,connection_id) REFERENCES mailbox_connections(tenant_id,id)
                ON DELETE CASCADE
        );
        CREATE TABLE mailbox_oauth_attempts (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), connection_id uuid NOT NULL,
            user_id uuid NOT NULL REFERENCES users(id), state_hash text NOT NULL UNIQUE,
            key_id text NOT NULL, nonce bytea NOT NULL, ciphertext bytea NOT NULL,
            expires_at timestamptz NOT NULL, consumed_at timestamptz,
            FOREIGN KEY(tenant_id,connection_id) REFERENCES mailbox_connections(tenant_id,id)
                ON DELETE CASCADE
        );
        CREATE TABLE mailbox_notification_receipts (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), connection_id uuid NOT NULL,
            receipt_hash text NOT NULL, UNIQUE(connection_id,receipt_hash),
            FOREIGN KEY(tenant_id,connection_id) REFERENCES mailbox_connections(tenant_id,id)
                ON DELETE CASCADE
        );
        CREATE FUNCTION mail_oauth_target(state_digest text)
            RETURNS TABLE(tenant_id uuid, connection_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT a.tenant_id, a.connection_id FROM public.mailbox_oauth_attempts a
            WHERE a.state_hash = state_digest AND a.user_id = public.actor_context()
                AND a.consumed_at IS NULL AND a.expires_at > now()
                AND public.tenant_is_member(a.tenant_id)
        $$;
        CREATE FUNCTION mail_notification_target(source text, lookup text)
            RETURNS TABLE(tenant_id uuid, id uuid, notification_secret_hash text,
            subscription_id text)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT m.tenant_id, m.id, m.notification_secret_hash, m.subscription_id
            FROM public.mailbox_connections m
            WHERE m.provider = source AND m.notification_lookup = lookup
                AND m.status = 'connected'
        $$;
        REVOKE ALL ON FUNCTION mail_oauth_target(text), mail_notification_target(text,text) FROM
        PUBLIC;
        GRANT EXECUTE ON FUNCTION mail_oauth_target(text) TO recruitment_api;
        GRANT EXECUTE ON FUNCTION mail_notification_target(text,text) TO recruitment_dispatcher;
    """)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY api_isolation ON {table} TO recruitment_api
            USING (tenant_id = tenant_context() AND tenant_is_member(tenant_id))
            WITH CHECK (tenant_id = tenant_context() AND tenant_is_member(tenant_id))""")
        op.execute(f"""CREATE POLICY worker_isolation ON {table} TO recruitment_worker
            USING (tenant_id = tenant_context()) WITH CHECK (tenant_id = tenant_context())""")
        op.execute(
            f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO recruitment_api,recruitment_worker"
        )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN IF EXISTS(SELECT 1 FROM mailbox_connections) THEN
            RAISE EXCEPTION 'Export and remove mailbox connections before downgrade';
        END IF; END $$;
    """)
    op.execute("DROP FUNCTION mail_oauth_target(text)")
    op.execute("DROP FUNCTION mail_notification_target(text,text)")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
