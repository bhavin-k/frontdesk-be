"""Better Auth credentials and sessions share the recruitment user identity."""

from alembic import op

revision = "0004_better_auth"
down_revision = "0003_jobs_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
        ALTER TABLE users ADD COLUMN name text NOT NULL DEFAULT '';
        ALTER TABLE users ADD COLUMN email_verified boolean NOT NULL DEFAULT false;
        ALTER TABLE users ADD COLUMN image text;
        ALTER TABLE users ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
        CREATE UNIQUE INDEX users_email_normalized ON users (lower(email));

        CREATE TABLE better_auth_accounts (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            account_id text NOT NULL, provider_id text NOT NULL,
            password text, access_token text, refresh_token text, id_token text,
            access_token_expires_at timestamptz, refresh_token_expires_at timestamptz,
            scope text, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
            UNIQUE (provider_id, account_id)
        );
        CREATE INDEX better_auth_accounts_user ON better_auth_accounts(user_id);
        CREATE TABLE better_auth_sessions (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token text NOT NULL UNIQUE, expires_at timestamptz NOT NULL,
            ip_address text, user_agent text,
            created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
        );
        CREATE INDEX better_auth_sessions_user ON better_auth_sessions(user_id);
        CREATE TABLE better_auth_verifications (
            id uuid PRIMARY KEY, identifier text NOT NULL, value text NOT NULL,
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
        );
        CREATE INDEX better_auth_verifications_identifier
            ON better_auth_verifications(identifier);
        CREATE TABLE better_auth_rate_limits (
            id uuid PRIMARY KEY, key text NOT NULL UNIQUE,
            count integer NOT NULL, last_request bigint NOT NULL
        );
    """)
    for table in (
        "users",
        "better_auth_accounts",
        "better_auth_sessions",
        "better_auth_verifications",
        "better_auth_rate_limits",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY authentication_service ON {table} TO recruitment_auth
            USING (true) WITH CHECK (true)
        """)
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO recruitment_auth")


def downgrade() -> None:
    # Restoring NOT NULL would discard accounts created through Better Auth.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM users WHERE password_hash IS NULL) THEN
                RAISE EXCEPTION 'Better Auth users exist; migrate credentials before downgrade';
            END IF;
        END $$;
        DROP TABLE better_auth_rate_limits, better_auth_verifications,
            better_auth_sessions, better_auth_accounts;
        DROP POLICY authentication_service ON users;
        ALTER TABLE users DISABLE ROW LEVEL SECURITY;
        ALTER TABLE users NO FORCE ROW LEVEL SECURITY;
        DROP INDEX users_email_normalized;
        ALTER TABLE users DROP COLUMN updated_at, DROP COLUMN image,
            DROP COLUMN email_verified, DROP COLUMN name;
        ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL;
    """)
