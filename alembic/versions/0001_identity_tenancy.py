"""Identity, one recruiter role, tenant bootstrap and forced row isolation."""

import os

import psycopg
from psycopg import sql

from alembic import op

revision = "0001_identity_tenancy"
down_revision = None
branch_labels = None
depends_on = None

ROLES = ("auth", "api", "worker", "dispatcher")


def upgrade() -> None:
    # Passwords come from deployment inputs, never from migration source or logs.
    raw = op.get_bind().connection.driver_connection
    assert isinstance(raw, psycopg.Connection)
    for name in ROLES:
        password = os.environ.get(f"RECRUITMENT_DB_{name.upper()}_PASSWORD")
        if not password or len(password) < 24:
            raise RuntimeError(f"Provide a generated RECRUITMENT_DB_{name.upper()}_PASSWORD.")
        raw.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(f"recruitment_{name}"), sql.Literal(password))
        )
    op.execute("""
        REVOKE CREATE ON SCHEMA public FROM PUBLIC;
        CREATE TABLE users (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            email varchar(320) NOT NULL UNIQUE, password_hash text NOT NULL
        );
        CREATE TABLE auth_sessions (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            user_id uuid NOT NULL REFERENCES users(id), token_hash varchar(64) NOT NULL UNIQUE,
            expires_at timestamptz NOT NULL, revoked_at timestamptz
        );
        CREATE INDEX ix_auth_sessions_user ON auth_sessions(user_id, expires_at);
        CREATE TABLE login_attempts (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL, identity_hash varchar(64) NOT NULL
        );
        CREATE INDEX ix_login_attempts_identity ON login_attempts(identity_hash, created_at);
        CREATE TABLE tenants (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            name varchar(200) NOT NULL, timezone text NOT NULL CHECK(timezone = 'Asia/Kolkata')
        );
        CREATE TABLE tenant_members (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES
                users(id),
            role text NOT NULL CHECK(role = 'recruiter'), UNIQUE(tenant_id, user_id)
        );
        CREATE TABLE tenant_retention_settings (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            tenant_id uuid NOT NULL UNIQUE REFERENCES tenants(id),
            rejected_retention_days integer NOT NULL CHECK(rejected_retention_days BETWEEN 1 AND
                3650),
            other_candidate_retention_days integer NOT NULL
                CHECK(other_candidate_retention_days BETWEEN 1 AND 3650)
        );
        CREATE FUNCTION tenant_context() RETURNS uuid
        LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
            SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
        $$;
        CREATE FUNCTION actor_context() RETURNS uuid
        LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
            SELECT nullif(current_setting('app.user_id', true), '')::uuid
        $$;
        CREATE FUNCTION tenant_is_member(target uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT EXISTS(SELECT 1 FROM public.tenant_members
                WHERE tenant_id = target AND user_id = public.actor_context())
        $$;
        CREATE FUNCTION my_memberships(actor uuid)
            RETURNS TABLE(tenant_id uuid, name text, role text)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT m.tenant_id, t.name::text, m.role FROM public.tenant_members m
            JOIN public.tenants t ON t.id = m.tenant_id WHERE m.user_id = actor
        $$;
        CREATE FUNCTION bootstrap_tenant(tenant_name text) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE tid uuid := gen_random_uuid(); actor uuid := public.actor_context();
        BEGIN
            IF actor IS NULL OR NOT EXISTS(SELECT 1 FROM public.users WHERE id = actor) THEN
                RAISE EXCEPTION 'Authenticated actor required' USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.tenants VALUES (tid, now(), tenant_name, 'Asia/Kolkata');
            INSERT INTO public.tenant_members VALUES
                (gen_random_uuid(), now(), tid, actor, 'recruiter');
            INSERT INTO public.tenant_retention_settings VALUES
                (gen_random_uuid(), now(), tid, 7, 30);
            RETURN tid;
        END $$;
        CREATE FUNCTION add_tenant_member(target uuid, member_id uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        BEGIN
            IF target IS DISTINCT FROM public.tenant_context()
               OR NOT public.tenant_is_member(target) THEN
                RAISE EXCEPTION 'Membership required' USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.tenant_members VALUES
                (gen_random_uuid(), now(), target, member_id, 'recruiter')
                ON CONFLICT (tenant_id, user_id) DO NOTHING;
        END $$;
        CREATE FUNCTION remove_tenant_member(target uuid, member_id uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        BEGIN
            IF target IS DISTINCT FROM public.tenant_context()
               OR NOT public.tenant_is_member(target) THEN
                RAISE EXCEPTION 'Membership required' USING ERRCODE = '42501';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(target::text, 0));
            IF (SELECT count(*) FROM public.tenant_members WHERE tenant_id = target) <= 1 THEN
                RETURN false;
            END IF;
            DELETE FROM public.tenant_members WHERE tenant_id = target AND user_id = member_id;
            RETURN true;
        END $$;
        REVOKE ALL ON FUNCTION tenant_is_member(uuid), my_memberships(uuid),
            bootstrap_tenant(text), add_tenant_member(uuid, uuid),
            remove_tenant_member(uuid, uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION tenant_is_member(uuid) TO recruitment_api;
        GRANT EXECUTE ON FUNCTION my_memberships(uuid) TO recruitment_auth;
        GRANT EXECUTE ON FUNCTION bootstrap_tenant(text), add_tenant_member(uuid, uuid),
            remove_tenant_member(uuid, uuid) TO recruitment_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON users, auth_sessions, login_attempts
            TO recruitment_auth;
        GRANT SELECT ON tenants, tenant_members, tenant_retention_settings TO recruitment_api;
        GRANT UPDATE ON tenant_retention_settings TO recruitment_api;
        GRANT SELECT ON tenants, tenant_retention_settings TO recruitment_worker;
    """)
    for table, column in (
        ("tenants", "id"),
        ("tenant_members", "tenant_id"),
        ("tenant_retention_settings", "tenant_id"),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY api_isolation ON {table} TO recruitment_api
            USING ({column} = tenant_context() AND tenant_is_member({column}))
            WITH CHECK ({column} = tenant_context() AND tenant_is_member({column}))
        """)
        op.execute(f"""
            CREATE POLICY worker_isolation ON {table} TO recruitment_worker
            USING ({column} = tenant_context()) WITH CHECK ({column} = tenant_context())
        """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION remove_tenant_member(uuid, uuid);
        DROP FUNCTION add_tenant_member(uuid, uuid);
        DROP FUNCTION bootstrap_tenant(text);
        DROP FUNCTION my_memberships(uuid);
        DROP TABLE tenant_retention_settings, tenant_members, tenants CASCADE;
        DROP FUNCTION tenant_is_member(uuid);
        DROP FUNCTION tenant_context();
        DROP FUNCTION actor_context();
        DROP TABLE login_attempts, auth_sessions, users;
    """)
    for name in reversed(ROLES):
        op.execute(f"DROP OWNED BY recruitment_{name}")
        op.execute(f"DROP ROLE recruitment_{name}")
