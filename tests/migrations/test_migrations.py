import os
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from scripts.migrate import upgrade_database
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
                ('uq_active_application','uq_opening_intake_current')
        """).fetchone()[0]
            == 2
        )
        assert connection.execute("""
            SELECT has_column_privilege(
                'recruitment_api', 'cv_uploads', 'retention_expires_at', 'UPDATE'
            ), has_column_privilege('recruitment_api', 'cv_uploads', 'status', 'UPDATE')
        """).fetchone() == (True, False)


def test_plain_postgres_bootstrap_has_no_vector_artifacts(clean_database, admin_url):
    with psycopg.connect(admin_url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0015_cv_json_matching",
        )
        assert not connection.execute(
            "SELECT 1 FROM pg_extension WHERE extname='vector'"
        ).fetchone()
        assert connection.execute(
            "SELECT to_regclass('resume_chunks'), to_regclass('cv_embedding_batches')"
        ).fetchone() == (None, None)
        assert not connection.execute("""
            SELECT 1 FROM information_schema.columns WHERE table_schema='public'
            AND (column_name LIKE 'embedding%' OR column_name='scoring_strategy')
        """).fetchone()


def test_models_match_reviewed_migrations(clean_database) -> None:
    command.check(Config(str(ROOT / "alembic.ini")))


def test_populated_0014_upgrade_keeps_history_and_resumes_pending_intake(monkeypatch):
    url = os.environ.get("RECRUITMENT_LEGACY_TEST_ADMIN_URL")
    if not url:
        pytest.skip("Supply a dedicated, empty Frontdesk 0014 upgrade fixture.")
    tenant, actor, opening, future = (uuid4() for _ in range(4))
    candidate, app, file_id, upload, resume, criteria, match = (uuid4() for _ in range(7))
    pending, pending_file, job = (uuid4() for _ in range(3))
    with psycopg.connect(url) as db:
        name, revision, empty = db.execute("""
            SELECT current_database(),version_num,
                NOT EXISTS (SELECT 1 FROM tenants) FROM alembic_version
        """).fetchone()
        assert name.startswith("frontdesk_0015_upgrade_")
        assert revision == "0014_full_text_scoring" and empty
        db.execute(
            """
            INSERT INTO users(id,created_at,email) VALUES(%s,now(),'upgrade@example.org')
        """,
            (actor,),
        )
        db.execute(
            """
            INSERT INTO tenants(id,created_at,name,timezone)
            VALUES(%s,now(),'Upgrade fixture','Asia/Kolkata')
        """,
            (tenant,),
        )
        db.execute(
            """
            INSERT INTO tenant_members(id,created_at,tenant_id,user_id,role)
            VALUES(gen_random_uuid(),now(),%s,%s,'recruiter')
        """,
            (tenant, actor),
        )
        db.execute(
            """
            INSERT INTO job_openings(id,created_at,tenant_id,title,job_description,status)
            VALUES(%s,now()-interval '2 days',%s,'Original role','Python development','active'),
                  (%s,now(),%s,'Future role','JavaScript development','active')
        """,
            (opening, tenant, future, tenant),
        )
        db.execute(
            """
            INSERT INTO opening_intake_periods(id,created_at,tenant_id,opening_id,starts_at)
            VALUES(gen_random_uuid(),now(),%s,%s,now()-interval '2 days'),
                  (gen_random_uuid(),now(),%s,%s,now())
        """,
            (tenant, opening, tenant, future),
        )
        db.execute(
            """
            INSERT INTO candidates(id,created_at,tenant_id,profile)
            VALUES(%s,now(),%s,'{"name":"Fictional Candidate"}')
        """,
            (candidate, tenant),
        )
        db.execute(
            """
            INSERT INTO applications(id,created_at,tenant_id,candidate_id,opening_id,status,
                                     last_activity_at,retention_expires_at,lock_version)
            VALUES(%s,now(),%s,%s,%s,'shortlisted',now(),now()+interval '7 days',1)
        """,
            (app, tenant, candidate, opening),
        )
        db.execute(
            """
            INSERT INTO resume_files(id,created_at,tenant_id,sha256,content,mime_type,byte_length)
            VALUES(%s,now(),%s,%s,%s,'application/pdf',7),
                  (%s,now(),%s,%s,%s,'application/pdf',7)
        """,
            (file_id, tenant, "a" * 64, b"tracked", pending_file, tenant, "b" * 64, b"pending"),
        )
        db.execute(
            """
            INSERT INTO cv_uploads(id,created_at,tenant_id,file_id,opening_id,application_id,status,
                                   retention_expires_at,source_received_at)
            VALUES(%s,now(),%s,%s,%s,%s,'completed',now()+interval '7 days',now()-interval '1 day'),
                  (%s,now(),%s,%s,%s,NULL,'embedding',now()+interval '7 days',
                   now()-interval '1 day')
        """,
            (upload, tenant, file_id, opening, app, pending, tenant, pending_file, opening),
        )
        db.execute(
            """
            INSERT INTO resumes(id,created_at,tenant_id,application_id,upload_id,version)
            VALUES(%s,now(),%s,%s,%s,1)
        """,
            (resume, tenant, app, upload),
        )
        db.execute(
            """
            INSERT INTO job_criteria_versions(id,created_at,tenant_id,opening_id,version,
                shortlist_threshold,source_revision)
            VALUES(%s,now(),%s,%s,1,75,1)
        """,
            (criteria, tenant, opening),
        )
        db.execute(
            """
            INSERT INTO match_runs(id,created_at,tenant_id,application_id,resume_id,
                criteria_version_id,recommendation,score,reason,model,provider,prompt_version,
                embedding_model,embedding_dimensions,embedding_fingerprint,
                missing_required_criteria,
                processed_at)
            VALUES(%s,now(),%s,%s,%s,%s,'recommended_shortlist',91,'Historical reason',
                'fake','fake','legacy','fake',1536,'legacy','[]',now())
        """,
            (match, tenant, app, resume, criteria),
        )
        db.execute(
            """
            INSERT INTO review_decisions(id,created_at,tenant_id,application_id,match_id,
                                         recruiter_id,decision)
            VALUES(gen_random_uuid(),now(),%s,%s,%s,%s,'shortlist')
        """,
            (tenant, app, match, actor),
        )
        db.execute(
            """
            UPDATE applications SET current_resume_id=%s,latest_match_id=%s WHERE id=%s
        """,
            (resume, match, app),
        )
        db.execute(
            """
            INSERT INTO resume_chunks(id,created_at,tenant_id,resume_id,position,source_spans,
                text,scoring_text,token_start,token_end,embedding,embedding_model,
                embedding_dimensions)
            VALUES(gen_random_uuid(),now(),%s,%s,0,'[]','Past text','Past text',0,2,%s,'fake',1536)
        """,
            (tenant, resume, "[" + ",".join(["1"] + ["0"] * 1535) + "]"),
        )
        db.execute(
            """
            INSERT INTO job_runs(id,created_at,tenant_id,kind,aggregate_id,dedupe_key,state,
                attempts,available_at,lease_token,lease_expires_at)
            VALUES(%s,now(),%s,'cv.uploaded',%s,'fixture:pending','running',1,now(),
                gen_random_uuid(),now()+interval '10 minutes')
        """,
            (job, tenant, pending),
        )

    target = make_url(url).set(drivername="postgresql+psycopg")
    monkeypatch.setenv("RECRUITMENT_MIGRATION_DATABASE_URL", target.render_as_string(False))
    upgrade_database()
    with psycopg.connect(url) as db:
        assert db.execute(
            "SELECT score,reason FROM match_runs WHERE id=%s", (match,)
        ).fetchone() == (
            91,
            "Historical reason",
        )
        assert db.execute("SELECT decision FROM review_decisions").fetchone() == ("shortlist",)
        assert db.execute(
            "SELECT content FROM resume_files WHERE id=%s", (file_id,)
        ).fetchone() == (b"tracked",)
        status, scope = db.execute(
            "SELECT status,matching_openings FROM cv_uploads WHERE id=%s", (pending,)
        ).fetchone()
        assert status == "queued" and [item["opening_id"] for item in scope] == [str(opening)]
        assert db.execute(
            "SELECT state,lease_token FROM job_runs WHERE id=%s", (job,)
        ).fetchone() == (
            "queued",
            None,
        )
        assert db.execute("SELECT to_regclass('resume_chunks')").fetchone() == (None,)
        assert not db.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()
