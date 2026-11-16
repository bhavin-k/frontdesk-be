import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from tests.conftest import ROOT


def test_database_has_forced_rls_utc_columns_and_safe_roles(clean_database, admin_url: str) -> None:
    with psycopg.connect(admin_url) as connection:
        owned = connection.execute("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
                AND c.relname NOT IN ('users','auth_sessions','login_attempts','alembic_version')
                AND c.relname NOT LIKE 'better_auth_%'
        """).fetchall()
        assert owned and all(enabled and forced for _, enabled, forced in owned)
        assert (
            connection.execute("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'public' AND data_type = 'timestamp without time zone'
        """).fetchone()[0]
            == 0
        )
        roles = connection.execute("""
            SELECT rolname, rolsuper, rolbypassrls FROM pg_roles
            WHERE rolname IN ('recruitment_api','recruitment_auth',
                             'recruitment_worker','recruitment_dispatcher')
        """).fetchall()
        assert len(roles) == 4 and all(
            not superuser and not bypass for _, superuser, bypass in roles
        )
        assert (
            connection.execute("""
            SELECT count(*) FROM pg_indexes WHERE indexname IN
                ('uq_active_application','uq_opening_intake_current','ix_chunk_vector')
        """).fetchone()[0]
            == 3
        )
        assert connection.execute("""
            SELECT has_column_privilege(
                'recruitment_api', 'cv_uploads', 'retention_expires_at', 'UPDATE'
            ), has_column_privilege('recruitment_api', 'cv_uploads', 'status', 'UPDATE')
        """).fetchone() == (True, False)


def test_fresh_install_and_seeded_upgrade(clean_database, admin_url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "base")
    command.upgrade(config, "0001_identity_tenancy")
    with psycopg.connect(admin_url) as connection:
        connection.execute("""
            INSERT INTO users(id, created_at, email, password_hash)
            VALUES (gen_random_uuid(), now(), 'migration-fixture@example.org', 'not-a-login-hash')
        """)
    command.upgrade(config, "head")
    with psycopg.connect(admin_url) as connection:
        assert (
            connection.execute(
                "SELECT email FROM users WHERE email = 'migration-fixture@example.org'"
            ).fetchone()[0]
            == "migration-fixture@example.org"
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            ScriptDirectory.from_config(config).get_current_head()
        )
    command.downgrade(config, "0002_cv_matching")
    command.upgrade(config, "head")
    command.downgrade(config, "0004_better_auth")
    command.upgrade(config, "head")


def test_models_match_reviewed_migrations(clean_database) -> None:
    command.check(Config(str(ROOT / "alembic.ini")))


def test_phase1_legacy_ownership_keys_upgrade_without_rewriting_history(clean_database, admin_url):
    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "0005_review_retention_permission")
    # Reproduce the weaker keys found in an earlier Phase 1 local installation.
    # All fixture DDL stays in the dedicated, empty test database.
    with psycopg.connect(admin_url) as connection:
        connection.execute("""
            ALTER TABLE applications DROP CONSTRAINT fk_latest_match;
            DO $$ DECLARE item record; BEGIN
                FOR item IN SELECT conrelid::regclass AS relation,conname FROM pg_constraint
                    WHERE contype='f' AND (
                        (conrelid='match_runs'::regclass AND confrelid='resumes'::regclass) OR
                        (conrelid='review_decisions'::regclass
                            AND confrelid='match_runs'::regclass))
                LOOP EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',item.relation,item.conname);
                END LOOP;
                FOR item IN SELECT conrelid::regclass AS relation,conname FROM pg_constraint
                    WHERE contype='u' AND conrelid IN ('resumes'::regclass,'match_runs'::regclass)
                        AND pg_get_constraintdef(oid)='UNIQUE (tenant_id, application_id, id)'
                LOOP EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',item.relation,item.conname);
                END LOOP;
            END $$;
            ALTER TABLE applications ADD CONSTRAINT fk_latest_match
                FOREIGN KEY(tenant_id,latest_match_id) REFERENCES match_runs(tenant_id,id)
                DEFERRABLE INITIALLY DEFERRED;
            ALTER TABLE match_runs ADD CONSTRAINT legacy_match_resume
                FOREIGN KEY(tenant_id,resume_id) REFERENCES resumes(tenant_id,id) ON DELETE CASCADE;
            ALTER TABLE review_decisions ADD CONSTRAINT legacy_review_match
                FOREIGN KEY(tenant_id,match_id) REFERENCES match_runs(tenant_id,id)
                    ON DELETE CASCADE;
        """)
    command.upgrade(config, "head")
    command.check(config)
    with psycopg.connect(admin_url) as connection:
        keys = connection.execute("""
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conname IN ('fk_latest_match','fk_match_resume_scope','fk_review_match_scope')
        """).fetchall()
        assert len(keys) == 3
        assert all("application_id" in definition for (definition,) in keys)


def test_populated_phase2_downgrade_refuses_to_discard_processing_records(context, cv, admin_url):
    context.upload(cv)
    context.drain()
    config = Config(str(ROOT / "alembic.ini"))
    with pytest.raises(
        Exception, match="Preserve (processing snapshots|opening setup|experience evaluations)"
    ):
        command.downgrade(config, "0005_review_retention_permission")
    with psycopg.connect(admin_url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            ScriptDirectory.from_config(config).get_current_head()
        )
        assert connection.execute("SELECT count(*) FROM cv_processing_snapshots").fetchone()[0] == 1


def test_mailbox_method_upgrade_preserves_oauth_and_candidate_records(
    clean_database, cv, admin_url
):
    import hashlib
    from uuid import uuid4

    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "0010_mail_thread_correlation")
    ids = {
        name: uuid4()
        for name in (
            "user",
            "tenant",
            "opening",
            "version",
            "candidate",
            "file",
            "app",
            "upload",
            "mailbox",
        )
    }
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            (
                "INSERT INTO users(id,created_at,email,password_hash) VALUES(%s,now(),"
                "'upgrade@example.org','not-a-login-hash')"
            ),
            (ids["user"],),
        )
        connection.execute(
            (
                "INSERT INTO tenants(id,created_at,name,timezone) VALUES(%s,now(),'Upgrade "
                "fixture','Asia/Kolkata')"
            ),
            (ids["tenant"],),
        )
        connection.execute(
            (
                "INSERT INTO job_openings(id,created_at,tenant_id,title,job_description,"
                "status) VALUES(%s,now(),%s,'Legacy job','Python development','active')"
            ),
            (ids["opening"], ids["tenant"]),
        )
        connection.execute(
            (
                "INSERT INTO job_criteria_versions(id,created_at,tenant_id,opening_id,"
                "version,shortlist_threshold) VALUES(%s,now(),%s,%s,1,83)"
            ),
            (ids["version"], ids["tenant"], ids["opening"]),
        )
        connection.execute(
            (
                "INSERT INTO job_criteria(id,created_at,tenant_id,version_id,position,text,"
                "required,strict,weight) VALUES(gen_random_uuid(),now(),%s,%s,0,'Python "
                "development',true,false,7)"
            ),
            (ids["tenant"], ids["version"]),
        )
        connection.execute(
            ("INSERT INTO candidates(id,created_at,tenant_id,profile) VALUES(%s,now(),%s,'{}')"),
            (ids["candidate"], ids["tenant"]),
        )
        connection.execute(
            (
                "INSERT INTO resume_files(id,created_at,tenant_id,sha256,content,mime_type,"
                "byte_length) VALUES(%s,now(),%s,%s,%s,'application/pdf',%s)"
            ),
            (ids["file"], ids["tenant"], hashlib.sha256(cv).hexdigest(), cv, len(cv)),
        )
        connection.execute(
            (
                "INSERT INTO applications(id,created_at,tenant_id,candidate_id,opening_id,"
                "status,lock_version,last_activity_at,retention_expires_at) VALUES(%s,"
                "now(),%s,%s,%s,'needs_review',1,now(),now()+interval '30 days')"
            ),
            (ids["app"], ids["tenant"], ids["candidate"], ids["opening"]),
        )
        connection.execute(
            (
                "INSERT INTO cv_uploads(id,created_at,tenant_id,file_id,opening_id,"
                "application_id,status,retention_expires_at,source_received_at) VALUES(%s,"
                "now(),%s,%s,%s,%s,'needs_review',now()+interval '30 days',now())"
            ),
            (ids["upload"], ids["tenant"], ids["file"], ids["opening"], ids["app"]),
        )
        connection.execute(
            (
                "INSERT INTO cv_processing_snapshots(id,created_at,tenant_id,upload_id,"
                "fingerprint,config) VALUES(gen_random_uuid(),now(),%s,%s,'legacy-fixture',"
                "'{}')"
            ),
            (ids["tenant"], ids["upload"]),
        )
        connection.execute(
            (
                "INSERT INTO mailbox_connections(id,created_at,tenant_id,provider,"
                "recruiter_id,opening_id,status,email,folder_id,intake_started_at,cursor,"
                "generation,source_period,sync_status) VALUES(%s,now(),%s,'gmail',%s,%s,"
                "'connected','migration@example.org','INBOX',now(),'{}',1,1,'idle')"
            ),
            (ids["mailbox"], ids["tenant"], ids["user"], ids["opening"]),
        )
    command.upgrade(config, "head")
    command.check(config)
    with psycopg.connect(admin_url) as connection:
        assert (
            connection.execute(
                "SELECT connection_method FROM mailbox_connections WHERE id=%s", (ids["mailbox"],)
            ).fetchone()[0]
            == "gmail_oauth"
        )
        for table in ("candidates", "applications", "cv_uploads", "cv_processing_snapshots"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        assert (
            connection.execute("SELECT shortlist_threshold FROM job_criteria_versions").fetchone()[
                0
            ]
            == 83
        )
        assert connection.execute("SELECT weight FROM job_criteria").fetchone()[0] == 7
        assert (
            connection.execute("SELECT criteria_status FROM job_openings").fetchone()[0] == "ready"
        )
        assert connection.execute("SELECT count(*) FROM opening_intake_periods").fetchone()[0] == 1


def test_method_downgrade_preserves_imap_connections(context, admin_url):
    from tests.integration.test_imap_connections import connect_imap

    mailbox = connect_imap(context)
    config = Config(str(ROOT / "alembic.ini"))
    with pytest.raises(Exception, match="Preserve (IMAP connections|opening setup)"):
        command.downgrade(config, "0010_mail_thread_correlation")
    with psycopg.connect(admin_url) as connection:
        assert (
            connection.execute(
                "SELECT connection_method FROM mailbox_connections WHERE id=%s", (mailbox["id"],)
            ).fetchone()[0]
            == "imap"
        )
