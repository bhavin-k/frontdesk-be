"""Explicit mailbox methods, shared address ownership and allowlisted polling."""

from alembic import op

revision = "0011_mailbox_connection_methods"
down_revision = "0010_mail_thread_correlation"
branch_labels = None
depends_on = None


def scheduler(methods: bool) -> None:
    signature = "enabled_methods text[], imap_seconds integer" if methods else ""
    allowed = "AND connection_method = ANY(enabled_methods)" if methods else ""
    interval = (
        "make_interval(secs => CASE WHEN item.connection_method='imap' "
        "THEN imap_seconds ELSE 900 END)"
        if methods
        else "interval '15 minutes'"
    )
    maintenance = "AND item.connection_method <> 'imap'" if methods else ""
    guard = (
        """
        IF imap_seconds NOT BETWEEN 30 AND 900 OR EXISTS(
            SELECT 1 FROM unnest(enabled_methods) m
            WHERE m NOT IN ('imap','gmail_oauth','microsoft_oauth')
        ) THEN RAISE EXCEPTION 'Invalid mailbox scheduling configuration'; END IF;
    """
        if methods
        else ""
    )
    op.execute(f"""
        CREATE FUNCTION schedule_mailboxes({signature}) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE item record; scheduled integer := 0;
        BEGIN
            {guard}
            FOR item IN SELECT * FROM public.mailbox_connections
                WHERE status='connected' {allowed}
                    AND (next_sync_at<=now() OR next_maintenance_at<=now())
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 100
            LOOP
                IF item.next_sync_at<=now() THEN
                    INSERT INTO public.outbox_messages
                        (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,available_at)
                    VALUES(gen_random_uuid(),now(),item.tenant_id,'mailbox.sync',item.id,
                        'mailbox.sync:'||item.id||':'||item.generation||':'||item.next_sync_at,now())
                        ON CONFLICT(dedupe_key) DO NOTHING;
                    UPDATE public.mailbox_connections SET next_sync_at=now()+{interval}
                        WHERE id=item.id;
                END IF;
                IF item.next_maintenance_at<=now() {maintenance} THEN
                    INSERT INTO public.outbox_messages
                        (id,created_at,tenant_id,kind,aggregate_id,dedupe_key,available_at)
                    VALUES(gen_random_uuid(),now(),item.tenant_id,'mailbox.maintain',item.id,
                        'mailbox.maintain:'||item.id||':'||item.generation||':'||
                            item.next_maintenance_at,now())
                        ON CONFLICT(dedupe_key) DO NOTHING;
                    UPDATE public.mailbox_connections
                        SET next_maintenance_at=now()+interval '1 hour'
                        WHERE id=item.id;
                END IF;
                scheduled := scheduled+1;
            END LOOP;
            DELETE FROM public.mailbox_oauth_attempts WHERE expires_at < now()-interval '1 day';
            DELETE FROM public.mailbox_notification_receipts
                WHERE created_at < now()-interval '1 day';
            RETURN scheduled;
        END $$;
    """)
    arguments = "text[],integer" if methods else ""
    op.execute(f"REVOKE ALL ON FUNCTION schedule_mailboxes({arguments}) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION schedule_mailboxes({arguments}) TO recruitment_dispatcher"
    )


def upgrade() -> None:
    op.execute("""
        ALTER TABLE mailbox_connections ADD COLUMN connection_method text,
            ADD COLUMN address_hash text;
        UPDATE mailbox_connections SET connection_method=provider||'_oauth';
        ALTER TABLE mailbox_connections ALTER COLUMN connection_method SET NOT NULL;
        ALTER TABLE mailbox_connections ADD CONSTRAINT ck_mailbox_connection_method
            CHECK ((provider='gmail' AND connection_method IN ('imap','gmail_oauth'))
                OR (provider='microsoft' AND connection_method='microsoft_oauth'));
        -- Share ownership across transports. Personal Gmail dot/plus aliases resolve
        -- to the same address. Never delete a conflicting pre-existing connection.
        UPDATE mailbox_connections SET address_hash=encode(sha256(convert_to(
            CASE WHEN lower(split_part(email,'@',2)) IN ('gmail.com','googlemail.com')
                THEN replace(split_part(lower(split_part(email,'@',1)),'+',1),'.','')||'@gmail.com'
                ELSE lower(trim(email)) END,'UTF8')),'hex') WHERE email IS NOT NULL;
        CREATE UNIQUE INDEX uq_active_mailbox_address ON mailbox_connections(address_hash)
            WHERE status <> 'disconnected' AND address_hash IS NOT NULL;
        DROP FUNCTION schedule_mailboxes();
    """)
    scheduler(True)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN IF EXISTS(SELECT 1 FROM mailbox_connections WHERE connection_method='imap')
            THEN RAISE EXCEPTION 'Preserve IMAP connections before downgrade'; END IF; END $$;
        DROP FUNCTION schedule_mailboxes(text[],integer);
        DROP INDEX uq_active_mailbox_address;
        ALTER TABLE mailbox_connections DROP CONSTRAINT ck_mailbox_connection_method,
            DROP COLUMN connection_method, DROP COLUMN address_hash;
    """)
    scheduler(False)
