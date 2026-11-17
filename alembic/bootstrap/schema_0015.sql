-- Frontdesk revision 0015 schema, generated from the forward migration on PostgreSQL 16.
CREATE FUNCTION public.actor_context() RETURNS uuid
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'public'
    AS $$
            SELECT nullif(current_setting('app.user_id', true), '')::uuid
        $$;

CREATE FUNCTION public.add_tenant_member(target uuid, member_id uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF target IS DISTINCT FROM public.tenant_context()
               OR NOT public.tenant_is_member(target) THEN
                RAISE EXCEPTION 'Membership required' USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.tenant_members VALUES
                (gen_random_uuid(), now(), target, member_id, 'recruiter')
                ON CONFLICT (tenant_id, user_id) DO NOTHING;
        END $$;

CREATE FUNCTION public.bootstrap_tenant(tenant_name text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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

CREATE FUNCTION public.claim_job(lease_seconds integer) RETURNS TABLE(id uuid, tenant_id uuid, kind text, aggregate_id uuid, lease_token uuid, attempts integer)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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

CREATE FUNCTION public.dispatch_outbox() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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

CREATE FUNCTION public.immutable_criteria() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'Published criteria are immutable' USING ERRCODE = '23514';
        END $$;

CREATE FUNCTION public.mail_notification_target(source text, lookup text) RETURNS TABLE(tenant_id uuid, id uuid, notification_secret_hash text, subscription_id text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
            SELECT m.tenant_id, m.id, m.notification_secret_hash, m.subscription_id
            FROM public.mailbox_connections m
            WHERE m.provider = source AND m.notification_lookup = lookup
                AND m.status = 'connected'
        $$;

CREATE FUNCTION public.mail_oauth_target(state_digest text) RETURNS TABLE(tenant_id uuid, connection_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
            SELECT a.tenant_id, a.connection_id FROM public.mailbox_oauth_attempts a
            WHERE a.state_hash = state_digest AND a.user_id = public.actor_context()
                AND a.consumed_at IS NULL AND a.expires_at > now()
                AND public.tenant_is_member(a.tenant_id)
        $$;

CREATE FUNCTION public.my_memberships(actor uuid) RETURNS TABLE(tenant_id uuid, name text, role text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
            SELECT m.tenant_id, t.name::text, m.role FROM public.tenant_members m
            JOIN public.tenants t ON t.id = m.tenant_id WHERE m.user_id = actor
        $$;

CREATE FUNCTION public.protect_current_match() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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

CREATE FUNCTION public.protect_rejection() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$ BEGIN
            IF TG_OP='UPDATE' AND OLD.status='rejected'
                AND current_user IN ('recruitment_api','recruitment_worker') AND (
                    NEW.status IS DISTINCT FROM OLD.status
                    OR NEW.rejected_at IS DISTINCT FROM OLD.rejected_at
                    OR NEW.latest_match_id IS DISTINCT FROM OLD.latest_match_id
                    OR NEW.latest_evaluation_id IS DISTINCT FROM OLD.latest_evaluation_id) THEN
                RAISE EXCEPTION 'Confirmed rejection is immutable' USING ERRCODE='42501';
            END IF;
            IF NEW.status='rejected' AND (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status)
            THEN

            IF current_user <> 'recruitment_api' THEN
                RAISE EXCEPTION 'Explicit recruiter decision required' USING ERRCODE='42501';
            END IF;

                IF NOT public.tenant_is_member(NEW.tenant_id)
                    OR NOT EXISTS(SELECT 1 FROM public.review_decisions
                        WHERE tenant_id=NEW.tenant_id AND application_id=NEW.id
                        AND ((NEW.latest_evaluation_id IS NULL AND match_id=NEW.latest_match_id)
                            OR (evaluation_id=NEW.latest_evaluation_id AND EXISTS(
                                SELECT 1 FROM public.matching_evaluations e WHERE e.id=evaluation_id
                                AND e.application_id=NEW.id AND e.resume_id=NEW.current_resume_id
                                AND e.state='completed')))
                        AND recruiter_id=public.actor_context() AND decision='reject'
                        AND created_at >= transaction_timestamp()) THEN
                    RAISE EXCEPTION 'Explicit recruiter decision required' USING ERRCODE='42501';
                END IF;
            END IF;
            IF NEW.status='shortlisted' AND NEW.latest_evaluation_id IS NOT NULL
                AND (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status
                     OR OLD.opening_id IS DISTINCT FROM NEW.opening_id) THEN

            IF current_user <> 'recruitment_api' THEN
                RAISE EXCEPTION 'Explicit recruiter selection required' USING ERRCODE='42501';
            END IF;

                IF NOT public.tenant_is_member(NEW.tenant_id) OR NOT EXISTS(
                    SELECT 1 FROM public.review_decisions d
                    JOIN public.role_assessments r ON r.id=d.role_assessment_id
                        AND r.evaluation_id=d.evaluation_id AND r.tenant_id=d.tenant_id
                    WHERE d.tenant_id=NEW.tenant_id AND d.application_id=NEW.id
                    AND d.evaluation_id=NEW.latest_evaluation_id
                    AND r.opening_id=NEW.opening_id AND d.decision='shortlist'
                    AND d.recruiter_id=public.actor_context()
                    AND d.created_at >= transaction_timestamp()
                ) THEN
                    RAISE EXCEPTION 'Explicit recruiter selection required' USING ERRCODE='42501';
                END IF;
            END IF;
            RETURN NEW;
        END $$;

CREATE FUNCTION public.remove_tenant_member(target uuid, member_id uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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

CREATE FUNCTION public.schedule_mail_retention() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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

CREATE FUNCTION public.schedule_mailboxes(enabled_methods text[], imap_seconds integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE item record; scheduled integer := 0;
        BEGIN

        IF imap_seconds NOT BETWEEN 30 AND 900 OR EXISTS(
            SELECT 1 FROM unnest(enabled_methods) m
            WHERE m NOT IN ('imap','gmail_oauth','microsoft_oauth')
        ) THEN RAISE EXCEPTION 'Invalid mailbox scheduling configuration'; END IF;

            FOR item IN SELECT * FROM public.mailbox_connections
                WHERE status='connected' AND connection_method = ANY(enabled_methods)
                    AND (next_sync_at<=now() OR next_maintenance_at<=now())
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 100
            LOOP
                IF item.next_sync_at<=now() THEN
                    INSERT INTO public.outbox_messages
                        (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,available_at)
                    VALUES(gen_random_uuid(),now(),item.tenant_id,'mailbox.sync',item.id,
                        'mailbox.sync:'||item.id||':'||item.generation||':'||item.next_sync_at,now())
                        ON CONFLICT(dedupe_key) DO NOTHING;
                    UPDATE public.mailbox_connections SET next_sync_at=now()+make_interval(secs => CASE WHEN item.connection_method='imap' THEN imap_seconds ELSE 900 END)
                        WHERE id=item.id;
                END IF;
                IF item.next_maintenance_at<=now() AND item.connection_method <> 'imap' THEN
                    INSERT INTO public.outbox_messages
                        (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,available_at)
                    VALUES(gen_random_uuid(),now(),item.tenant_id,'mailbox.maintain',item.id,
                        'mailbox.maintain:'||item.id||':'||item.generation||':'||
                            item.next_maintenance_at,now())
                        ON CONFLICT(dedupe_key) DO NOTHING;
                    UPDATE public.mailbox_connections SET next_maintenance_at=now()+interval '1 hour'
                        WHERE id=item.id;
                END IF;
                scheduled := scheduled+1;
            END LOOP;
            DELETE FROM public.mailbox_oauth_attempts WHERE expires_at < now()-interval '1 day';
            DELETE FROM public.mailbox_notification_receipts
                WHERE created_at < now()-interval '1 day';
            RETURN scheduled;
        END $$;

CREATE FUNCTION public.schedule_retention() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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

CREATE FUNCTION public.tenant_context() RETURNS uuid
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'public'
    AS $$
            SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
        $$;

CREATE FUNCTION public.tenant_is_member(target uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
            SELECT EXISTS(SELECT 1 FROM public.tenant_members
                WHERE tenant_id = target AND user_id = public.actor_context())
        $$;

CREATE TABLE public.applications (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    opening_id uuid NOT NULL,
    status text NOT NULL,
    latest_match_id uuid,
    last_activity_at timestamp with time zone NOT NULL,
    rejected_at timestamp with time zone,
    retention_expires_at timestamp with time zone NOT NULL,
    lock_version integer NOT NULL,
    current_resume_id uuid,
    latest_evaluation_id uuid,
    CONSTRAINT applications_check CHECK (((status = 'rejected'::text) = (rejected_at IS NOT NULL))),
    CONSTRAINT applications_lock_version_check CHECK ((lock_version > 0)),
    CONSTRAINT applications_status_check CHECK ((status = ANY (ARRAY['processing'::text, 'needs_review'::text, 'shortlisted'::text, 'rejected'::text])))
);

ALTER TABLE ONLY public.applications FORCE ROW LEVEL SECURITY;

CREATE TABLE public.auth_sessions (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone
);

CREATE TABLE public.better_auth_accounts (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    account_id text NOT NULL,
    provider_id text NOT NULL,
    password text,
    access_token text,
    refresh_token text,
    id_token text,
    access_token_expires_at timestamp with time zone,
    refresh_token_expires_at timestamp with time zone,
    scope text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);

ALTER TABLE ONLY public.better_auth_accounts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.better_auth_rate_limits (
    id uuid NOT NULL,
    key text NOT NULL,
    count integer NOT NULL,
    last_request bigint NOT NULL
);

ALTER TABLE ONLY public.better_auth_rate_limits FORCE ROW LEVEL SECURITY;

CREATE TABLE public.better_auth_sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    ip_address text,
    user_agent text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);

ALTER TABLE ONLY public.better_auth_sessions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.better_auth_verifications (
    id uuid NOT NULL,
    identifier text NOT NULL,
    value text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);

ALTER TABLE ONLY public.better_auth_verifications FORCE ROW LEVEL SECURITY;

CREATE TABLE public.candidate_contact_revisions (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    revision integer NOT NULL,
    actor_id uuid NOT NULL,
    before json NOT NULL,
    after json NOT NULL,
    reason text NOT NULL,
    CONSTRAINT candidate_contact_revisions_reason_check CHECK ((length(reason) > 0)),
    CONSTRAINT candidate_contact_revisions_revision_check CHECK ((revision > 0))
);

ALTER TABLE ONLY public.candidate_contact_revisions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.candidate_contacts (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    kind text NOT NULL,
    value text NOT NULL,
    CONSTRAINT candidate_contacts_kind_check CHECK ((kind = ANY (ARRAY['email'::text, 'mobile'::text])))
);

ALTER TABLE ONLY public.candidate_contacts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.candidate_data_tombstones (
    tenant_id uuid NOT NULL,
    application_id uuid NOT NULL,
    role_id uuid NOT NULL,
    final_status text NOT NULL,
    deleted_at timestamp with time zone NOT NULL
);

ALTER TABLE ONLY public.candidate_data_tombstones FORCE ROW LEVEL SECURITY;

CREATE TABLE public.candidate_identity_reviews (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    upload_id uuid,
    message_id uuid,
    candidate_id uuid,
    kind text NOT NULL,
    status text NOT NULL,
    proposed_contacts json NOT NULL,
    candidate_options json NOT NULL,
    decision text,
    reason text,
    actor_id uuid,
    resolved_at timestamp with time zone,
    target_opening_id uuid,
    CONSTRAINT candidate_identity_reviews_check CHECK (((upload_id IS NOT NULL) OR (message_id IS NOT NULL))),
    CONSTRAINT candidate_identity_reviews_kind_check CHECK ((kind = ANY (ARRAY['identity_conflict'::text, 'contact_proposal'::text, 'contact_issue'::text, 'cross_role_submission'::text]))),
    CONSTRAINT candidate_identity_reviews_status_check CHECK ((status = ANY (ARRAY['open'::text, 'resolved'::text])))
);

ALTER TABLE ONLY public.candidate_identity_reviews FORCE ROW LEVEL SECURITY;

CREATE TABLE public.candidates (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    profile json NOT NULL,
    current_source_at timestamp with time zone,
    current_source_key text,
    contact_revision integer DEFAULT 1 NOT NULL,
    contact_overrides json DEFAULT '{}'::json NOT NULL,
    CONSTRAINT candidates_contact_revision_check CHECK ((contact_revision > 0))
);

ALTER TABLE ONLY public.candidates FORCE ROW LEVEL SECURITY;

CREATE TABLE public.criterion_results (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    match_id uuid NOT NULL,
    criterion_id uuid NOT NULL,
    status text NOT NULL,
    confidence numeric(5,4) NOT NULL,
    reason text NOT NULL,
    CONSTRAINT criterion_results_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT criterion_results_status_check CHECK ((status = ANY (ARRAY['met'::text, 'partial'::text, 'missing'::text, 'unknown'::text])))
);

ALTER TABLE ONLY public.criterion_results FORCE ROW LEVEL SECURITY;

CREATE TABLE public.cv_uploads (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    file_id uuid NOT NULL,
    opening_id uuid NOT NULL,
    application_id uuid,
    status text NOT NULL,
    review_reason text,
    retention_expires_at timestamp with time zone NOT NULL,
    source_received_at timestamp with time zone NOT NULL,
    resolved_candidate_id uuid,
    identity_choice text,
    matching_openings json DEFAULT '[]'::json NOT NULL,
    CONSTRAINT cv_uploads_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'extracting'::text, 'profiling'::text, 'matching'::text, 'retry_wait'::text, 'completed'::text, 'needs_review'::text])))
);

ALTER TABLE ONLY public.cv_uploads FORCE ROW LEVEL SECURITY;

CREATE TABLE public.document_derivatives (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    upload_id uuid NOT NULL,
    content bytea NOT NULL,
    sha256 text NOT NULL,
    converter_version text NOT NULL,
    CONSTRAINT document_derivatives_content_check CHECK (((octet_length(content) >= 1) AND (octet_length(content) <= 20971520)))
);

ALTER TABLE ONLY public.document_derivatives FORCE ROW LEVEL SECURITY;

CREATE TABLE public.failed_jobs (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    job_id uuid NOT NULL,
    aggregate_id uuid NOT NULL,
    failure_code text NOT NULL
);

ALTER TABLE ONLY public.failed_jobs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.idempotency_records (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    scope text NOT NULL,
    key character varying(128) NOT NULL,
    fingerprint character varying(64) NOT NULL,
    resource_id uuid NOT NULL
);

ALTER TABLE ONLY public.idempotency_records FORCE ROW LEVEL SECURITY;

CREATE TABLE public.job_criteria (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    version_id uuid NOT NULL,
    "position" integer NOT NULL,
    text text NOT NULL,
    required boolean NOT NULL,
    strict boolean NOT NULL,
    weight numeric(8,3) NOT NULL,
    source_evidence json DEFAULT '[]'::json NOT NULL,
    CONSTRAINT job_criteria_check CHECK (((NOT strict) OR required)),
    CONSTRAINT job_criteria_weight_check CHECK ((weight > (0)::numeric))
);

ALTER TABLE ONLY public.job_criteria FORCE ROW LEVEL SECURITY;

CREATE TABLE public.job_criteria_versions (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    opening_id uuid NOT NULL,
    version integer NOT NULL,
    shortlist_threshold numeric(5,2) NOT NULL,
    source_revision integer NOT NULL,
    processing_metadata json DEFAULT '{}'::json NOT NULL,
    CONSTRAINT job_criteria_versions_shortlist_threshold_check CHECK (((shortlist_threshold >= (0)::numeric) AND (shortlist_threshold <= (100)::numeric))),
    CONSTRAINT job_criteria_versions_version_check CHECK ((version > 0))
);

ALTER TABLE ONLY public.job_criteria_versions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.job_openings (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    job_description text NOT NULL,
    status text NOT NULL,
    details json DEFAULT '{}'::json NOT NULL,
    questions json DEFAULT '[]'::json NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    source_revision integer DEFAULT 1 NOT NULL,
    CONSTRAINT job_openings_details_check CHECK ((json_typeof(details) = 'object'::text)),
    CONSTRAINT job_openings_questions_check CHECK (((json_typeof(questions) = 'array'::text) AND (json_array_length(questions) <= 30))),
    CONSTRAINT job_openings_revision_check CHECK ((revision > 0)),
    CONSTRAINT job_openings_source_revision_check CHECK ((source_revision > 0)),
    CONSTRAINT job_openings_status_check CHECK ((status = ANY (ARRAY['active'::text, 'paused'::text, 'closed'::text])))
);

ALTER TABLE ONLY public.job_openings FORCE ROW LEVEL SECURITY;

CREATE TABLE public.job_runs (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    kind text NOT NULL,
    aggregate_id uuid NOT NULL,
    dedupe_key text NOT NULL,
    state text NOT NULL,
    attempts integer NOT NULL,
    available_at timestamp with time zone NOT NULL,
    lease_token uuid,
    lease_expires_at timestamp with time zone,
    failure_code text,
    CONSTRAINT job_runs_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT job_runs_check CHECK (((state = 'running'::text) = ((lease_token IS NOT NULL) AND (lease_expires_at IS NOT NULL)))),
    CONSTRAINT job_runs_state_check CHECK ((state = ANY (ARRAY['queued'::text, 'running'::text, 'retry_wait'::text, 'succeeded'::text, 'failed'::text])))
);

ALTER TABLE ONLY public.job_runs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.login_attempts (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    identity_hash character varying(64) NOT NULL
);

CREATE TABLE public.mail_source_attachments (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    message_id uuid NOT NULL,
    provider_attachment_id text NOT NULL,
    filename text NOT NULL,
    mime_type text NOT NULL,
    byte_length integer NOT NULL,
    upload_id uuid,
    status text NOT NULL,
    failure_code text,
    CONSTRAINT mail_source_attachments_byte_length_check CHECK ((byte_length >= 0)),
    CONSTRAINT mail_source_attachments_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'processing'::text, 'retry_wait'::text, 'completed'::text, 'needs_review'::text])))
);

ALTER TABLE ONLY public.mail_source_attachments FORCE ROW LEVEL SECURITY;

CREATE TABLE public.mail_source_messages (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    provider_message_id text NOT NULL,
    internet_message_id text,
    received_at timestamp with time zone NOT NULL,
    generation integer NOT NULL,
    provider_thread_id text,
    CONSTRAINT mail_source_messages_generation_check CHECK ((generation > 0))
);

ALTER TABLE ONLY public.mail_source_messages FORCE ROW LEVEL SECURITY;

CREATE TABLE public.mailbox_connections (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    provider text NOT NULL,
    recruiter_id uuid NOT NULL,
    opening_id uuid NOT NULL,
    status text NOT NULL,
    email text,
    owner_hash text,
    notification_lookup text,
    folder_id text NOT NULL,
    intake_started_at timestamp with time zone,
    completed_watermark timestamp with time zone,
    cursor json NOT NULL,
    generation integer NOT NULL,
    source_period integer NOT NULL,
    sync_status text NOT NULL,
    failure_code text,
    last_success_at timestamp with time zone,
    next_sync_at timestamp with time zone,
    next_maintenance_at timestamp with time zone,
    subscription_id text,
    subscription_expires_at timestamp with time zone,
    notification_secret_hash text,
    lease_token uuid,
    lease_expires_at timestamp with time zone,
    connection_method text NOT NULL,
    address_hash text,
    CONSTRAINT ck_mailbox_connection_method CHECK ((((provider = 'gmail'::text) AND (connection_method = ANY (ARRAY['imap'::text, 'gmail_oauth'::text]))) OR ((provider = 'microsoft'::text) AND (connection_method = 'microsoft_oauth'::text)))),
    CONSTRAINT mailbox_connections_check CHECK (((lease_token IS NULL) = (lease_expires_at IS NULL))),
    CONSTRAINT mailbox_connections_check1 CHECK (((status <> 'connected'::text) OR (intake_started_at IS NOT NULL))),
    CONSTRAINT mailbox_connections_generation_check CHECK ((generation > 0)),
    CONSTRAINT mailbox_connections_provider_check CHECK ((provider = ANY (ARRAY['gmail'::text, 'microsoft'::text]))),
    CONSTRAINT mailbox_connections_source_period_check CHECK ((source_period > 0)),
    CONSTRAINT mailbox_connections_status_check CHECK ((status = ANY (ARRAY['pending_authorization'::text, 'needs_configuration'::text, 'connected'::text, 'reauthorization_required'::text, 'disconnecting'::text, 'disconnected'::text, 'error'::text]))),
    CONSTRAINT mailbox_connections_sync_status_check CHECK ((sync_status = ANY (ARRAY['idle'::text, 'queued'::text, 'running'::text, 'retry_wait'::text, 'failed'::text])))
);

ALTER TABLE ONLY public.mailbox_connections FORCE ROW LEVEL SECURITY;

CREATE TABLE public.mailbox_credentials (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    key_id text NOT NULL,
    nonce bytea NOT NULL,
    ciphertext bytea NOT NULL
);

ALTER TABLE ONLY public.mailbox_credentials FORCE ROW LEVEL SECURITY;

CREATE TABLE public.mailbox_notification_receipts (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    receipt_hash text NOT NULL
);

ALTER TABLE ONLY public.mailbox_notification_receipts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.mailbox_oauth_attempts (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    user_id uuid NOT NULL,
    state_hash text NOT NULL,
    key_id text NOT NULL,
    nonce bytea NOT NULL,
    ciphertext bytea NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone
);

ALTER TABLE ONLY public.mailbox_oauth_attempts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.match_runs (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    application_id uuid NOT NULL,
    resume_id uuid NOT NULL,
    criteria_version_id uuid NOT NULL,
    recommendation text NOT NULL,
    score numeric(5,2),
    reason text NOT NULL,
    model text NOT NULL,
    provider text NOT NULL,
    prompt_version text NOT NULL,
    missing_required_criteria json NOT NULL,
    processed_at timestamp with time zone NOT NULL,
    policy_version text DEFAULT 'legacy-v1'::text NOT NULL,
    CONSTRAINT match_runs_recommendation_check CHECK ((recommendation = ANY (ARRAY['recommended_shortlist'::text, 'recommended_rejection'::text, 'needs_review'::text]))),
    CONSTRAINT match_runs_score_check CHECK (((score >= (0)::numeric) AND (score <= (100)::numeric)))
);

ALTER TABLE ONLY public.match_runs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.matching_evaluations (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    application_id uuid NOT NULL,
    resume_id uuid NOT NULL,
    signature text NOT NULL,
    role_snapshot json NOT NULL,
    config json NOT NULL,
    state text NOT NULL,
    recommendation text NOT NULL,
    reason text NOT NULL,
    confidence numeric(5,4),
    recommended_assessment_id uuid,
    processed_at timestamp with time zone,
    CONSTRAINT matching_evaluations_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT matching_evaluations_recommendation_check CHECK ((recommendation = ANY (ARRAY['recommended_shortlist'::text, 'recommended_rejection'::text, 'needs_review'::text]))),
    CONSTRAINT matching_evaluations_state_check CHECK ((state = ANY (ARRAY['processing'::text, 'completed'::text, 'superseded'::text])))
);

ALTER TABLE ONLY public.matching_evaluations FORCE ROW LEVEL SECURITY;

CREATE TABLE public.opening_documents (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    opening_id uuid NOT NULL,
    file_name text NOT NULL,
    mime_type text NOT NULL,
    byte_length integer NOT NULL,
    sha256 text NOT NULL,
    content bytea NOT NULL,
    CONSTRAINT opening_documents_byte_length_check CHECK (((byte_length >= 1) AND (byte_length <= 10485760))),
    CONSTRAINT opening_documents_check CHECK ((byte_length = octet_length(content)))
);

ALTER TABLE ONLY public.opening_documents FORCE ROW LEVEL SECURITY;

CREATE TABLE public.opening_intake_periods (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    opening_id uuid NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone,
    match_description json NOT NULL,
    CONSTRAINT opening_intake_periods_check CHECK (((ends_at IS NULL) OR (ends_at >= starts_at)))
);

ALTER TABLE ONLY public.opening_intake_periods FORCE ROW LEVEL SECURITY;

CREATE TABLE public.outbox_messages (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    kind text NOT NULL,
    aggregate_id uuid NOT NULL,
    dedupe_key text NOT NULL,
    available_at timestamp with time zone NOT NULL,
    dispatched_at timestamp with time zone
);

ALTER TABLE ONLY public.outbox_messages FORCE ROW LEVEL SECURITY;

CREATE TABLE public.resume_extractions (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    upload_id uuid NOT NULL,
    pages json NOT NULL,
    method text NOT NULL,
    confidence numeric(5,4) NOT NULL,
    profile json NOT NULL,
    profile_model text NOT NULL,
    prompt_version text NOT NULL,
    CONSTRAINT resume_extractions_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))
);

ALTER TABLE ONLY public.resume_extractions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.resume_files (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    sha256 character varying(64) NOT NULL,
    content bytea NOT NULL,
    mime_type text NOT NULL,
    byte_length integer NOT NULL,
    CONSTRAINT resume_files_byte_length_check CHECK (((byte_length >= 1) AND (byte_length <= 10485760))),
    CONSTRAINT resume_files_check CHECK ((octet_length(content) = byte_length)),
    CONSTRAINT resume_files_supported_mime CHECK ((mime_type = ANY (ARRAY['application/pdf'::text, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'::text, 'application/msword'::text, 'image/jpeg'::text, 'image/png'::text])))
);

ALTER TABLE ONLY public.resume_files FORCE ROW LEVEL SECURITY;

CREATE TABLE public.resumes (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    application_id uuid NOT NULL,
    upload_id uuid NOT NULL,
    version integer NOT NULL,
    CONSTRAINT resumes_version_check CHECK ((version > 0))
);

ALTER TABLE ONLY public.resumes FORCE ROW LEVEL SECURITY;

CREATE TABLE public.review_decisions (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    application_id uuid NOT NULL,
    match_id uuid,
    recruiter_id uuid NOT NULL,
    decision text NOT NULL,
    reason text,
    evaluation_id uuid,
    role_assessment_id uuid,
    CONSTRAINT ck_decision_assessment CHECK (((role_assessment_id IS NULL) OR (evaluation_id IS NOT NULL))),
    CONSTRAINT ck_decision_reference CHECK (((match_id IS NOT NULL) OR (evaluation_id IS NOT NULL))),
    CONSTRAINT review_decisions_decision_check CHECK ((decision = ANY (ARRAY['shortlist'::text, 'reject'::text])))
);

ALTER TABLE ONLY public.review_decisions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.role_assessments (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    evaluation_id uuid NOT NULL,
    application_id uuid NOT NULL,
    opening_id uuid NOT NULL,
    criteria_version_id uuid,
    state text NOT NULL,
    recommendation text NOT NULL,
    confidence numeric(5,4),
    score numeric(5,2),
    provisional boolean DEFAULT false NOT NULL,
    reason text NOT NULL,
    match_run_id uuid,
    failure_code text,
    match_result json,
    CONSTRAINT role_assessments_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT role_assessments_recommendation_check CHECK ((recommendation = ANY (ARRAY['recommended_shortlist'::text, 'recommended_rejection'::text, 'needs_review'::text]))),
    CONSTRAINT role_assessments_score_check CHECK (((score >= (0)::numeric) AND (score <= (100)::numeric))),
    CONSTRAINT role_assessments_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'processing'::text, 'completed'::text, 'failed'::text])))
);

ALTER TABLE ONLY public.role_assessments FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenant_members (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role text NOT NULL,
    CONSTRAINT tenant_members_role_check CHECK ((role = 'recruiter'::text))
);

ALTER TABLE ONLY public.tenant_members FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenant_retention_settings (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    rejected_retention_days integer NOT NULL,
    other_candidate_retention_days integer NOT NULL,
    CONSTRAINT tenant_retention_settings_other_candidate_retention_days_check CHECK (((other_candidate_retention_days >= 1) AND (other_candidate_retention_days <= 3650))),
    CONSTRAINT tenant_retention_settings_rejected_retention_days_check CHECK (((rejected_retention_days >= 1) AND (rejected_retention_days <= 3650)))
);

ALTER TABLE ONLY public.tenant_retention_settings FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenants (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    name character varying(200) NOT NULL,
    timezone text NOT NULL,
    CONSTRAINT tenants_timezone_check CHECK ((timezone = 'Asia/Kolkata'::text))
);

ALTER TABLE ONLY public.tenants FORCE ROW LEVEL SECURITY;

CREATE TABLE public.users (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    email character varying(320) NOT NULL,
    password_hash text,
    name text DEFAULT ''::text NOT NULL,
    email_verified boolean DEFAULT false NOT NULL,
    image text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.users FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.applications
    ADD CONSTRAINT applications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.applications
    ADD CONSTRAINT applications_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_token_hash_key UNIQUE (token_hash);

ALTER TABLE ONLY public.better_auth_accounts
    ADD CONSTRAINT better_auth_accounts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.better_auth_accounts
    ADD CONSTRAINT better_auth_accounts_provider_id_account_id_key UNIQUE (provider_id, account_id);

ALTER TABLE ONLY public.better_auth_rate_limits
    ADD CONSTRAINT better_auth_rate_limits_key_key UNIQUE (key);

ALTER TABLE ONLY public.better_auth_rate_limits
    ADD CONSTRAINT better_auth_rate_limits_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.better_auth_sessions
    ADD CONSTRAINT better_auth_sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.better_auth_sessions
    ADD CONSTRAINT better_auth_sessions_token_key UNIQUE (token);

ALTER TABLE ONLY public.better_auth_verifications
    ADD CONSTRAINT better_auth_verifications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.candidate_contact_revisions
    ADD CONSTRAINT candidate_contact_revisions_candidate_id_revision_key UNIQUE (candidate_id, revision);

ALTER TABLE ONLY public.candidate_contact_revisions
    ADD CONSTRAINT candidate_contact_revisions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.candidate_contacts
    ADD CONSTRAINT candidate_contacts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.candidate_contacts
    ADD CONSTRAINT candidate_contacts_tenant_id_kind_value_key UNIQUE (tenant_id, kind, value);

ALTER TABLE ONLY public.candidate_data_tombstones
    ADD CONSTRAINT candidate_data_tombstones_pkey PRIMARY KEY (tenant_id, application_id);

ALTER TABLE ONLY public.candidate_identity_reviews
    ADD CONSTRAINT candidate_identity_reviews_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.candidate_identity_reviews
    ADD CONSTRAINT candidate_identity_reviews_upload_id_target_opening_id_key UNIQUE (upload_id, target_opening_id);

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.criterion_results
    ADD CONSTRAINT criterion_results_match_id_criterion_id_key UNIQUE (match_id, criterion_id);

ALTER TABLE ONLY public.criterion_results
    ADD CONSTRAINT criterion_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cv_uploads
    ADD CONSTRAINT cv_uploads_file_id_key UNIQUE (file_id);

ALTER TABLE ONLY public.cv_uploads
    ADD CONSTRAINT cv_uploads_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cv_uploads
    ADD CONSTRAINT cv_uploads_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.document_derivatives
    ADD CONSTRAINT document_derivatives_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_derivatives
    ADD CONSTRAINT document_derivatives_upload_id_key UNIQUE (upload_id);

ALTER TABLE ONLY public.failed_jobs
    ADD CONSTRAINT failed_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT idempotency_records_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT idempotency_records_tenant_id_scope_key_key UNIQUE (tenant_id, scope, key);

ALTER TABLE ONLY public.job_criteria
    ADD CONSTRAINT job_criteria_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.job_criteria
    ADD CONSTRAINT job_criteria_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.job_criteria
    ADD CONSTRAINT job_criteria_version_id_position_key UNIQUE (version_id, "position");

ALTER TABLE ONLY public.job_criteria_versions
    ADD CONSTRAINT job_criteria_versions_opening_id_source_revision_key UNIQUE (opening_id, source_revision);

ALTER TABLE ONLY public.job_criteria_versions
    ADD CONSTRAINT job_criteria_versions_opening_id_version_key UNIQUE (opening_id, version);

ALTER TABLE ONLY public.job_criteria_versions
    ADD CONSTRAINT job_criteria_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.job_criteria_versions
    ADD CONSTRAINT job_criteria_versions_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.job_openings
    ADD CONSTRAINT job_openings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.job_openings
    ADD CONSTRAINT job_openings_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.job_runs
    ADD CONSTRAINT job_runs_dedupe_key_key UNIQUE (dedupe_key);

ALTER TABLE ONLY public.job_runs
    ADD CONSTRAINT job_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.job_runs
    ADD CONSTRAINT job_runs_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.login_attempts
    ADD CONSTRAINT login_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mail_source_attachments
    ADD CONSTRAINT mail_source_attachments_message_id_provider_attachment_id_key UNIQUE (message_id, provider_attachment_id);

ALTER TABLE ONLY public.mail_source_attachments
    ADD CONSTRAINT mail_source_attachments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mail_source_attachments
    ADD CONSTRAINT mail_source_attachments_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.mail_source_messages
    ADD CONSTRAINT mail_source_messages_connection_id_provider_message_id_key UNIQUE (connection_id, provider_message_id);

ALTER TABLE ONLY public.mail_source_messages
    ADD CONSTRAINT mail_source_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mail_source_messages
    ADD CONSTRAINT mail_source_messages_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.mailbox_connections
    ADD CONSTRAINT mailbox_connections_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mailbox_connections
    ADD CONSTRAINT mailbox_connections_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.mailbox_credentials
    ADD CONSTRAINT mailbox_credentials_connection_id_key UNIQUE (connection_id);

ALTER TABLE ONLY public.mailbox_credentials
    ADD CONSTRAINT mailbox_credentials_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mailbox_notification_receipts
    ADD CONSTRAINT mailbox_notification_receipts_connection_id_receipt_hash_key UNIQUE (connection_id, receipt_hash);

ALTER TABLE ONLY public.mailbox_notification_receipts
    ADD CONSTRAINT mailbox_notification_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mailbox_oauth_attempts
    ADD CONSTRAINT mailbox_oauth_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mailbox_oauth_attempts
    ADD CONSTRAINT mailbox_oauth_attempts_state_hash_key UNIQUE (state_hash);

ALTER TABLE ONLY public.match_runs
    ADD CONSTRAINT match_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.match_runs
    ADD CONSTRAINT match_runs_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.matching_evaluations
    ADD CONSTRAINT matching_evaluations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.matching_evaluations
    ADD CONSTRAINT matching_evaluations_resume_id_signature_key UNIQUE (resume_id, signature);

ALTER TABLE ONLY public.matching_evaluations
    ADD CONSTRAINT matching_evaluations_tenant_id_application_id_id_key UNIQUE (tenant_id, application_id, id);

ALTER TABLE ONLY public.matching_evaluations
    ADD CONSTRAINT matching_evaluations_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.opening_documents
    ADD CONSTRAINT opening_documents_opening_id_sha256_key UNIQUE (opening_id, sha256);

ALTER TABLE ONLY public.opening_documents
    ADD CONSTRAINT opening_documents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.opening_documents
    ADD CONSTRAINT opening_documents_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.opening_intake_periods
    ADD CONSTRAINT opening_intake_periods_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.outbox_messages
    ADD CONSTRAINT outbox_messages_dedupe_key_key UNIQUE (dedupe_key);

ALTER TABLE ONLY public.outbox_messages
    ADD CONSTRAINT outbox_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.resume_extractions
    ADD CONSTRAINT resume_extractions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.resume_extractions
    ADD CONSTRAINT resume_extractions_upload_id_key UNIQUE (upload_id);

ALTER TABLE ONLY public.resume_files
    ADD CONSTRAINT resume_files_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.resume_files
    ADD CONSTRAINT resume_files_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.resume_files
    ADD CONSTRAINT resume_files_tenant_id_sha256_key UNIQUE (tenant_id, sha256);

ALTER TABLE ONLY public.resumes
    ADD CONSTRAINT resumes_application_id_version_key UNIQUE (application_id, version);

ALTER TABLE ONLY public.resumes
    ADD CONSTRAINT resumes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.resumes
    ADD CONSTRAINT resumes_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.resumes
    ADD CONSTRAINT resumes_upload_id_key UNIQUE (upload_id);

ALTER TABLE ONLY public.review_decisions
    ADD CONSTRAINT review_decisions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.role_assessments
    ADD CONSTRAINT role_assessments_evaluation_id_opening_id_key UNIQUE (evaluation_id, opening_id);

ALTER TABLE ONLY public.role_assessments
    ADD CONSTRAINT role_assessments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.role_assessments
    ADD CONSTRAINT role_assessments_tenant_id_evaluation_id_id_key UNIQUE (tenant_id, evaluation_id, id);

ALTER TABLE ONLY public.role_assessments
    ADD CONSTRAINT role_assessments_tenant_id_id_key UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.tenant_members
    ADD CONSTRAINT tenant_members_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tenant_members
    ADD CONSTRAINT tenant_members_tenant_id_user_id_key UNIQUE (tenant_id, user_id);

ALTER TABLE ONLY public.tenant_retention_settings
    ADD CONSTRAINT tenant_retention_settings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tenant_retention_settings
    ADD CONSTRAINT tenant_retention_settings_tenant_id_key UNIQUE (tenant_id);

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.match_runs
    ADD CONSTRAINT uq_match_application_scope UNIQUE (tenant_id, application_id, id);

ALTER TABLE ONLY public.match_runs
    ADD CONSTRAINT uq_match_policy UNIQUE (resume_id, criteria_version_id, policy_version);

ALTER TABLE ONLY public.resumes
    ADD CONSTRAINT uq_resume_application_scope UNIQUE (tenant_id, application_id, id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);

CREATE INDEX better_auth_accounts_user ON public.better_auth_accounts USING btree (user_id);

CREATE INDEX better_auth_sessions_user ON public.better_auth_sessions USING btree (user_id);

CREATE INDEX better_auth_verifications_identifier ON public.better_auth_verifications USING btree (identifier);

CREATE INDEX ix_application_expiry ON public.applications USING btree (tenant_id, retention_expires_at);

CREATE INDEX ix_auth_sessions_user ON public.auth_sessions USING btree (user_id, expires_at);

CREATE INDEX ix_job_claim ON public.job_runs USING btree (available_at, lease_expires_at) WHERE (state = ANY (ARRAY['queued'::text, 'retry_wait'::text, 'running'::text]));

CREATE INDEX ix_login_attempts_identity ON public.login_attempts USING btree (identity_hash, created_at);

CREATE INDEX ix_mail_message_retention ON public.mail_source_messages USING btree (tenant_id, received_at);

CREATE INDEX ix_mail_message_thread ON public.mail_source_messages USING btree (connection_id, provider_thread_id);

CREATE INDEX ix_mailbox_schedule ON public.mailbox_connections USING btree (next_sync_at, next_maintenance_at);

CREATE INDEX ix_opening_intake_received ON public.opening_intake_periods USING btree (opening_id, starts_at, ends_at);

CREATE INDEX ix_outbox_pending ON public.outbox_messages USING btree (available_at) WHERE (dispatched_at IS NULL);

CREATE INDEX ix_role_assessment_evaluation ON public.role_assessments USING btree (tenant_id, evaluation_id);

CREATE INDEX ix_upload_review ON public.cv_uploads USING btree (tenant_id, status, created_at);

CREATE UNIQUE INDEX uq_active_application ON public.applications USING btree (tenant_id, candidate_id) WHERE (rejected_at IS NULL);

CREATE UNIQUE INDEX uq_active_mailbox_address ON public.mailbox_connections USING btree (address_hash) WHERE ((status <> 'disconnected'::text) AND (address_hash IS NOT NULL));

CREATE UNIQUE INDEX uq_active_mailbox_owner ON public.mailbox_connections USING btree (provider, owner_hash) WHERE ((status <> 'disconnected'::text) AND (owner_hash IS NOT NULL));

CREATE UNIQUE INDEX uq_identity_upload ON public.candidate_identity_reviews USING btree (upload_id) WHERE (kind <> 'cross_role_submission'::text);

CREATE UNIQUE INDEX uq_opening_intake_current ON public.opening_intake_periods USING btree (opening_id) WHERE (ends_at IS NULL);

CREATE UNIQUE INDEX users_email_normalized ON public.users USING btree (lower((email)::text));

CREATE TRIGGER application_current_match_guard BEFORE UPDATE ON public.applications FOR EACH ROW EXECUTE FUNCTION public.protect_current_match();

CREATE TRIGGER application_rejection_guard BEFORE INSERT OR UPDATE ON public.applications FOR EACH ROW EXECUTE FUNCTION public.protect_rejection();

CREATE TRIGGER criteria_version_immutable BEFORE UPDATE ON public.job_criteria_versions FOR EACH ROW EXECUTE FUNCTION public.immutable_criteria();

ALTER TABLE ONLY public.applications
    ADD CONSTRAINT applications_tenant_id_candidate_id_fkey FOREIGN KEY (tenant_id, candidate_id) REFERENCES public.candidates(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.applications
    ADD CONSTRAINT applications_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.applications
    ADD CONSTRAINT applications_tenant_id_opening_id_fkey FOREIGN KEY (tenant_id, opening_id) REFERENCES public.job_openings(tenant_id, id);

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.better_auth_accounts
    ADD CONSTRAINT better_auth_accounts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.better_auth_sessions
    ADD CONSTRAINT better_auth_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.candidate_contact_revisions
    ADD CONSTRAINT candidate_contact_revisions_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.candidate_contact_revisions
    ADD CONSTRAINT candidate_contact_revisions_tenant_id_candidate_id_fkey FOREIGN KEY (tenant_id, candidate_id) REFERENCES public.candidates(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.candidate_contact_revisions
    ADD CONSTRAINT candidate_contact_revisions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.candidate_contacts
    ADD CONSTRAINT candidate_contacts_tenant_id_candidate_id_fkey FOREIGN KEY (tenant_id, candidate_id) REFERENCES public.candidates(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.candidate_contacts
    ADD CONSTRAINT candidate_contacts_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.candidate_data_tombstones
    ADD CONSTRAINT candidate_data_tombstones_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.candidate_identity_reviews
    ADD CONSTRAINT candidate_identity_reviews_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.candidate_identity_reviews
    ADD CONSTRAINT candidate_identity_reviews_tenant_id_candidate_id_fkey FOREIGN KEY (tenant_id, candidate_id) REFERENCES public.candidates(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.candidate_identity_reviews
    ADD CONSTRAINT candidate_identity_reviews_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.candidate_identity_reviews
    ADD CONSTRAINT candidate_identity_reviews_tenant_id_message_id_fkey FOREIGN KEY (tenant_id, message_id) REFERENCES public.mail_source_messages(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.candidate_identity_reviews
    ADD CONSTRAINT candidate_identity_reviews_tenant_id_target_opening_id_fkey FOREIGN KEY (tenant_id, target_opening_id) REFERENCES public.job_openings(tenant_id, id);

ALTER TABLE ONLY public.candidate_identity_reviews
    ADD CONSTRAINT candidate_identity_reviews_tenant_id_upload_id_fkey FOREIGN KEY (tenant_id, upload_id) REFERENCES public.cv_uploads(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.criterion_results
    ADD CONSTRAINT criterion_results_tenant_id_criterion_id_fkey FOREIGN KEY (tenant_id, criterion_id) REFERENCES public.job_criteria(tenant_id, id);

ALTER TABLE ONLY public.criterion_results
    ADD CONSTRAINT criterion_results_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.criterion_results
    ADD CONSTRAINT criterion_results_tenant_id_match_id_fkey FOREIGN KEY (tenant_id, match_id) REFERENCES public.match_runs(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cv_uploads
    ADD CONSTRAINT cv_uploads_tenant_id_application_id_fkey FOREIGN KEY (tenant_id, application_id) REFERENCES public.applications(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cv_uploads
    ADD CONSTRAINT cv_uploads_tenant_id_file_id_fkey FOREIGN KEY (tenant_id, file_id) REFERENCES public.resume_files(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cv_uploads
    ADD CONSTRAINT cv_uploads_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.cv_uploads
    ADD CONSTRAINT cv_uploads_tenant_id_opening_id_fkey FOREIGN KEY (tenant_id, opening_id) REFERENCES public.job_openings(tenant_id, id);

ALTER TABLE ONLY public.cv_uploads
    ADD CONSTRAINT cv_uploads_tenant_id_resolved_candidate_id_fkey FOREIGN KEY (tenant_id, resolved_candidate_id) REFERENCES public.candidates(tenant_id, id);

ALTER TABLE ONLY public.document_derivatives
    ADD CONSTRAINT document_derivatives_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.document_derivatives
    ADD CONSTRAINT document_derivatives_tenant_id_upload_id_fkey FOREIGN KEY (tenant_id, upload_id) REFERENCES public.cv_uploads(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.failed_jobs
    ADD CONSTRAINT failed_jobs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.role_assessments
    ADD CONSTRAINT fk_assessment_match FOREIGN KEY (tenant_id, application_id, match_run_id) REFERENCES public.match_runs(tenant_id, application_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.applications
    ADD CONSTRAINT fk_current_resume FOREIGN KEY (tenant_id, id, current_resume_id) REFERENCES public.resumes(tenant_id, application_id, id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE ONLY public.applications
    ADD CONSTRAINT fk_latest_evaluation FOREIGN KEY (tenant_id, id, latest_evaluation_id) REFERENCES public.matching_evaluations(tenant_id, application_id, id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE ONLY public.applications
    ADD CONSTRAINT fk_latest_match FOREIGN KEY (tenant_id, id, latest_match_id) REFERENCES public.match_runs(tenant_id, application_id, id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE ONLY public.match_runs
    ADD CONSTRAINT fk_match_resume_scope FOREIGN KEY (tenant_id, application_id, resume_id) REFERENCES public.resumes(tenant_id, application_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.matching_evaluations
    ADD CONSTRAINT fk_recommended_assessment FOREIGN KEY (tenant_id, id, recommended_assessment_id) REFERENCES public.role_assessments(tenant_id, evaluation_id, id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE ONLY public.review_decisions
    ADD CONSTRAINT fk_review_match_scope FOREIGN KEY (tenant_id, application_id, match_id) REFERENCES public.match_runs(tenant_id, application_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT idempotency_records_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.job_criteria
    ADD CONSTRAINT job_criteria_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.job_criteria
    ADD CONSTRAINT job_criteria_tenant_id_version_id_fkey FOREIGN KEY (tenant_id, version_id) REFERENCES public.job_criteria_versions(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.job_criteria_versions
    ADD CONSTRAINT job_criteria_versions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.job_criteria_versions
    ADD CONSTRAINT job_criteria_versions_tenant_id_opening_id_fkey FOREIGN KEY (tenant_id, opening_id) REFERENCES public.job_openings(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.job_openings
    ADD CONSTRAINT job_openings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.job_runs
    ADD CONSTRAINT job_runs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.mail_source_attachments
    ADD CONSTRAINT mail_source_attachments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.mail_source_attachments
    ADD CONSTRAINT mail_source_attachments_tenant_id_message_id_fkey FOREIGN KEY (tenant_id, message_id) REFERENCES public.mail_source_messages(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.mail_source_attachments
    ADD CONSTRAINT mail_source_attachments_tenant_id_upload_id_fkey FOREIGN KEY (tenant_id, upload_id) REFERENCES public.cv_uploads(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.mail_source_messages
    ADD CONSTRAINT mail_source_messages_tenant_id_connection_id_fkey FOREIGN KEY (tenant_id, connection_id) REFERENCES public.mailbox_connections(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.mail_source_messages
    ADD CONSTRAINT mail_source_messages_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.mailbox_connections
    ADD CONSTRAINT mailbox_connections_recruiter_id_fkey FOREIGN KEY (recruiter_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.mailbox_connections
    ADD CONSTRAINT mailbox_connections_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.mailbox_connections
    ADD CONSTRAINT mailbox_connections_tenant_id_opening_id_fkey FOREIGN KEY (tenant_id, opening_id) REFERENCES public.job_openings(tenant_id, id);

ALTER TABLE ONLY public.mailbox_credentials
    ADD CONSTRAINT mailbox_credentials_tenant_id_connection_id_fkey FOREIGN KEY (tenant_id, connection_id) REFERENCES public.mailbox_connections(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.mailbox_credentials
    ADD CONSTRAINT mailbox_credentials_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.mailbox_notification_receipts
    ADD CONSTRAINT mailbox_notification_receipts_tenant_id_connection_id_fkey FOREIGN KEY (tenant_id, connection_id) REFERENCES public.mailbox_connections(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.mailbox_notification_receipts
    ADD CONSTRAINT mailbox_notification_receipts_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.mailbox_oauth_attempts
    ADD CONSTRAINT mailbox_oauth_attempts_tenant_id_connection_id_fkey FOREIGN KEY (tenant_id, connection_id) REFERENCES public.mailbox_connections(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.mailbox_oauth_attempts
    ADD CONSTRAINT mailbox_oauth_attempts_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.mailbox_oauth_attempts
    ADD CONSTRAINT mailbox_oauth_attempts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.match_runs
    ADD CONSTRAINT match_runs_tenant_id_application_id_fkey FOREIGN KEY (tenant_id, application_id) REFERENCES public.applications(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.match_runs
    ADD CONSTRAINT match_runs_tenant_id_criteria_version_id_fkey FOREIGN KEY (tenant_id, criteria_version_id) REFERENCES public.job_criteria_versions(tenant_id, id);

ALTER TABLE ONLY public.match_runs
    ADD CONSTRAINT match_runs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.matching_evaluations
    ADD CONSTRAINT matching_evaluations_tenant_id_application_id_fkey FOREIGN KEY (tenant_id, application_id) REFERENCES public.applications(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.matching_evaluations
    ADD CONSTRAINT matching_evaluations_tenant_id_application_id_resume_id_fkey FOREIGN KEY (tenant_id, application_id, resume_id) REFERENCES public.resumes(tenant_id, application_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.matching_evaluations
    ADD CONSTRAINT matching_evaluations_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.opening_documents
    ADD CONSTRAINT opening_documents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.opening_documents
    ADD CONSTRAINT opening_documents_tenant_id_opening_id_fkey FOREIGN KEY (tenant_id, opening_id) REFERENCES public.job_openings(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.opening_intake_periods
    ADD CONSTRAINT opening_intake_periods_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.opening_intake_periods
    ADD CONSTRAINT opening_intake_periods_tenant_id_opening_id_fkey FOREIGN KEY (tenant_id, opening_id) REFERENCES public.job_openings(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.outbox_messages
    ADD CONSTRAINT outbox_messages_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.resume_extractions
    ADD CONSTRAINT resume_extractions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.resume_extractions
    ADD CONSTRAINT resume_extractions_tenant_id_upload_id_fkey FOREIGN KEY (tenant_id, upload_id) REFERENCES public.cv_uploads(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.resume_files
    ADD CONSTRAINT resume_files_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.resumes
    ADD CONSTRAINT resumes_tenant_id_application_id_fkey FOREIGN KEY (tenant_id, application_id) REFERENCES public.applications(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.resumes
    ADD CONSTRAINT resumes_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.resumes
    ADD CONSTRAINT resumes_tenant_id_upload_id_fkey FOREIGN KEY (tenant_id, upload_id) REFERENCES public.cv_uploads(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.review_decisions
    ADD CONSTRAINT review_decisions_recruiter_id_fkey FOREIGN KEY (recruiter_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.review_decisions
    ADD CONSTRAINT review_decisions_tenant_id_application_id_evaluation_id_fkey FOREIGN KEY (tenant_id, application_id, evaluation_id) REFERENCES public.matching_evaluations(tenant_id, application_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.review_decisions
    ADD CONSTRAINT review_decisions_tenant_id_application_id_fkey FOREIGN KEY (tenant_id, application_id) REFERENCES public.applications(tenant_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.review_decisions
    ADD CONSTRAINT review_decisions_tenant_id_evaluation_id_role_assessment_i_fkey FOREIGN KEY (tenant_id, evaluation_id, role_assessment_id) REFERENCES public.role_assessments(tenant_id, evaluation_id, id);

ALTER TABLE ONLY public.review_decisions
    ADD CONSTRAINT review_decisions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.role_assessments
    ADD CONSTRAINT role_assessments_tenant_id_application_id_evaluation_id_fkey FOREIGN KEY (tenant_id, application_id, evaluation_id) REFERENCES public.matching_evaluations(tenant_id, application_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.role_assessments
    ADD CONSTRAINT role_assessments_tenant_id_criteria_version_id_fkey FOREIGN KEY (tenant_id, criteria_version_id) REFERENCES public.job_criteria_versions(tenant_id, id);

ALTER TABLE ONLY public.role_assessments
    ADD CONSTRAINT role_assessments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.role_assessments
    ADD CONSTRAINT role_assessments_tenant_id_opening_id_fkey FOREIGN KEY (tenant_id, opening_id) REFERENCES public.job_openings(tenant_id, id);

ALTER TABLE ONLY public.tenant_members
    ADD CONSTRAINT tenant_members_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.tenant_members
    ADD CONSTRAINT tenant_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.tenant_retention_settings
    ADD CONSTRAINT tenant_retention_settings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

CREATE POLICY api_isolation ON public.applications TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.candidate_contact_revisions TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.candidate_contacts TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.candidate_data_tombstones TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.candidate_identity_reviews TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.candidates TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.criterion_results TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.cv_uploads TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.document_derivatives TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.failed_jobs TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.idempotency_records TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.job_criteria TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.job_criteria_versions TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.job_openings TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.job_runs TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.mail_source_attachments TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.mail_source_messages TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.mailbox_connections TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.mailbox_credentials TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.mailbox_notification_receipts TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.mailbox_oauth_attempts TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.match_runs TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.matching_evaluations TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.opening_documents TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.opening_intake_periods TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.outbox_messages TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.resume_extractions TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.resume_files TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.resumes TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.review_decisions TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.role_assessments TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.tenant_members TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.tenant_retention_settings TO recruitment_api USING (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id))) WITH CHECK (((tenant_id = public.tenant_context()) AND public.tenant_is_member(tenant_id)));

CREATE POLICY api_isolation ON public.tenants TO recruitment_api USING (((id = public.tenant_context()) AND public.tenant_is_member(id))) WITH CHECK (((id = public.tenant_context()) AND public.tenant_is_member(id)));

ALTER TABLE public.applications ENABLE ROW LEVEL SECURITY;

CREATE POLICY authentication_service ON public.better_auth_accounts TO recruitment_auth USING (true) WITH CHECK (true);

CREATE POLICY authentication_service ON public.better_auth_rate_limits TO recruitment_auth USING (true) WITH CHECK (true);

CREATE POLICY authentication_service ON public.better_auth_sessions TO recruitment_auth USING (true) WITH CHECK (true);

CREATE POLICY authentication_service ON public.better_auth_verifications TO recruitment_auth USING (true) WITH CHECK (true);

CREATE POLICY authentication_service ON public.users TO recruitment_auth USING (true) WITH CHECK (true);

ALTER TABLE public.better_auth_accounts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.better_auth_rate_limits ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.better_auth_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.better_auth_verifications ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.candidate_contact_revisions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.candidate_contacts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.candidate_data_tombstones ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.candidate_identity_reviews ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.candidates ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.criterion_results ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.cv_uploads ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.document_derivatives ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.failed_jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.idempotency_records ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.job_criteria ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.job_criteria_versions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.job_openings ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.job_runs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.mail_source_attachments ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.mail_source_messages ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.mailbox_connections ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.mailbox_credentials ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.mailbox_notification_receipts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.mailbox_oauth_attempts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.match_runs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.matching_evaluations ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.opening_documents ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.opening_intake_periods ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.outbox_messages ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.resume_extractions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.resume_files ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.resumes ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.review_decisions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.role_assessments ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.tenant_members ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.tenant_retention_settings ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

CREATE POLICY worker_isolation ON public.applications TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.candidate_contact_revisions TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.candidate_contacts TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.candidate_data_tombstones TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.candidate_identity_reviews TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.candidates TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.criterion_results TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.cv_uploads TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.document_derivatives TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.failed_jobs TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.idempotency_records TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.job_criteria TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.job_criteria_versions TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.job_openings TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.job_runs TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.mail_source_attachments TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.mail_source_messages TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.mailbox_connections TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.mailbox_credentials TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.mailbox_notification_receipts TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.mailbox_oauth_attempts TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.match_runs TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.matching_evaluations TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.opening_documents TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.opening_intake_periods TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.outbox_messages TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.resume_extractions TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.resume_files TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.resumes TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.review_decisions TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.role_assessments TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.tenant_members TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.tenant_retention_settings TO recruitment_worker USING ((tenant_id = public.tenant_context())) WITH CHECK ((tenant_id = public.tenant_context()));

CREATE POLICY worker_isolation ON public.tenants TO recruitment_worker USING ((id = public.tenant_context())) WITH CHECK ((id = public.tenant_context()));

GRANT USAGE ON SCHEMA public TO recruitment_api;
GRANT USAGE ON SCHEMA public TO recruitment_worker;

REVOKE ALL ON FUNCTION public.add_tenant_member(target uuid, member_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.add_tenant_member(target uuid, member_id uuid) TO recruitment_api;

REVOKE ALL ON FUNCTION public.bootstrap_tenant(tenant_name text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.bootstrap_tenant(tenant_name text) TO recruitment_api;

REVOKE ALL ON FUNCTION public.claim_job(lease_seconds integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.claim_job(lease_seconds integer) TO recruitment_dispatcher;

REVOKE ALL ON FUNCTION public.dispatch_outbox() FROM PUBLIC;
GRANT ALL ON FUNCTION public.dispatch_outbox() TO recruitment_dispatcher;

REVOKE ALL ON FUNCTION public.mail_notification_target(source text, lookup text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.mail_notification_target(source text, lookup text) TO recruitment_dispatcher;

REVOKE ALL ON FUNCTION public.mail_oauth_target(state_digest text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.mail_oauth_target(state_digest text) TO recruitment_api;

REVOKE ALL ON FUNCTION public.my_memberships(actor uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.my_memberships(actor uuid) TO recruitment_auth;

REVOKE ALL ON FUNCTION public.remove_tenant_member(target uuid, member_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.remove_tenant_member(target uuid, member_id uuid) TO recruitment_api;

REVOKE ALL ON FUNCTION public.schedule_mail_retention() FROM PUBLIC;
GRANT ALL ON FUNCTION public.schedule_mail_retention() TO recruitment_dispatcher;

REVOKE ALL ON FUNCTION public.schedule_mailboxes(enabled_methods text[], imap_seconds integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.schedule_mailboxes(enabled_methods text[], imap_seconds integer) TO recruitment_dispatcher;

REVOKE ALL ON FUNCTION public.schedule_retention() FROM PUBLIC;
GRANT ALL ON FUNCTION public.schedule_retention() TO recruitment_dispatcher;

REVOKE ALL ON FUNCTION public.tenant_is_member(target uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tenant_is_member(target uuid) TO recruitment_api;

GRANT SELECT,UPDATE ON TABLE public.applications TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.applications TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.auth_sessions TO recruitment_auth;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.better_auth_accounts TO recruitment_auth;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.better_auth_rate_limits TO recruitment_auth;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.better_auth_sessions TO recruitment_auth;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.better_auth_verifications TO recruitment_auth;

GRANT SELECT,INSERT,UPDATE ON TABLE public.candidate_contact_revisions TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidate_contact_revisions TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidate_contacts TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidate_contacts TO recruitment_worker;

GRANT SELECT ON TABLE public.candidate_data_tombstones TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidate_data_tombstones TO recruitment_worker;

GRANT SELECT,INSERT,UPDATE ON TABLE public.candidate_identity_reviews TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidate_identity_reviews TO recruitment_worker;

GRANT SELECT,INSERT ON TABLE public.candidates TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidates TO recruitment_worker;

GRANT UPDATE(profile) ON TABLE public.candidates TO recruitment_api;

GRANT UPDATE(contact_revision) ON TABLE public.candidates TO recruitment_api;

GRANT UPDATE(contact_overrides) ON TABLE public.candidates TO recruitment_api;

GRANT SELECT ON TABLE public.criterion_results TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.criterion_results TO recruitment_worker;

GRANT SELECT,INSERT ON TABLE public.cv_uploads TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.cv_uploads TO recruitment_worker;

GRANT UPDATE(retention_expires_at) ON TABLE public.cv_uploads TO recruitment_api;

GRANT UPDATE(resolved_candidate_id) ON TABLE public.cv_uploads TO recruitment_api;

GRANT UPDATE(identity_choice) ON TABLE public.cv_uploads TO recruitment_api;

GRANT SELECT ON TABLE public.document_derivatives TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_derivatives TO recruitment_worker;

GRANT SELECT ON TABLE public.failed_jobs TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.failed_jobs TO recruitment_worker;

GRANT SELECT,INSERT ON TABLE public.idempotency_records TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.idempotency_records TO recruitment_worker;

GRANT SELECT ON TABLE public.job_criteria TO recruitment_api;
GRANT SELECT ON TABLE public.job_criteria TO recruitment_worker;

GRANT SELECT ON TABLE public.job_criteria_versions TO recruitment_api;
GRANT SELECT ON TABLE public.job_criteria_versions TO recruitment_worker;

GRANT SELECT,INSERT,UPDATE ON TABLE public.job_openings TO recruitment_api;
GRANT SELECT,UPDATE ON TABLE public.job_openings TO recruitment_worker;

GRANT SELECT ON TABLE public.job_runs TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.job_runs TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.login_attempts TO recruitment_auth;

GRANT SELECT ON TABLE public.mail_source_attachments TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mail_source_attachments TO recruitment_worker;

GRANT SELECT ON TABLE public.mail_source_messages TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mail_source_messages TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mailbox_connections TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mailbox_connections TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mailbox_credentials TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mailbox_credentials TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mailbox_notification_receipts TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mailbox_notification_receipts TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mailbox_oauth_attempts TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mailbox_oauth_attempts TO recruitment_worker;

GRANT SELECT ON TABLE public.match_runs TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.match_runs TO recruitment_worker;

GRANT SELECT ON TABLE public.matching_evaluations TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.matching_evaluations TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.opening_documents TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.opening_documents TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.opening_intake_periods TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.opening_intake_periods TO recruitment_worker;

GRANT SELECT,INSERT ON TABLE public.outbox_messages TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.outbox_messages TO recruitment_worker;

GRANT SELECT ON TABLE public.resume_extractions TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.resume_extractions TO recruitment_worker;

GRANT SELECT,INSERT ON TABLE public.resume_files TO recruitment_api;
GRANT SELECT,INSERT,DELETE ON TABLE public.resume_files TO recruitment_worker;

GRANT SELECT ON TABLE public.resumes TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.resumes TO recruitment_worker;

GRANT SELECT,INSERT ON TABLE public.review_decisions TO recruitment_api;
GRANT SELECT,DELETE ON TABLE public.review_decisions TO recruitment_worker;

GRANT SELECT ON TABLE public.role_assessments TO recruitment_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.role_assessments TO recruitment_worker;

GRANT SELECT ON TABLE public.tenant_members TO recruitment_api;

GRANT SELECT,UPDATE ON TABLE public.tenant_retention_settings TO recruitment_api;
GRANT SELECT ON TABLE public.tenant_retention_settings TO recruitment_worker;

GRANT SELECT ON TABLE public.tenants TO recruitment_api;
GRANT SELECT ON TABLE public.tenants TO recruitment_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.users TO recruitment_auth;

ALTER TABLE ONLY public.candidate_contacts ADD UNIQUE (tenant_id, candidate_id, kind);
