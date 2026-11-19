import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from openai import OpenAI
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import Application, Tenant
from recruitment.adapters.db.phase2 import Mailbox
from recruitment.adapters.db.screening import (
    ScreeningInbox,
    ScreeningMessage,
    ScreeningRequest,
    ScreeningSlot,
    ScreeningThread,
)
from recruitment.adapters.mail_runtime import MailboxRuntime
from recruitment.adapters.screening_ai import (
    FakeScreeningClassifier,
    ScreeningReplyClassifier,
    ScreeningReplyDrafter,
)
from recruitment.adapters.screening_availability import ConfigAvailabilityProvider
from recruitment.core.errors import AppError, ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.profiles import normalize_email
from recruitment.domain.screening import BOOKING_WINDOW, REPLY_LIMIT, TERMINAL
from recruitment.domain.screening_text import acknowledgment, candidate_reply
from recruitment.ports.queue import Lease, WorkerQueue
from recruitment.ports.screening import (
    AvailabilityProvider,
    EmailProvider,
    IncomingEmail,
    OutgoingEmail,
    ReplyClassifier,
    ReplyDrafter,
)
from recruitment.services.common import emit
from recruitment.services.mailboxes import read_credentials
from recruitment.services.screening_conversation import (
    ScreeningConversation,
    referenced_offer,
    show_time,
)
from recruitment.services.screening_records import (
    audit,
    booking,
    queue_email,
    release_holds,
    screening_mailbox_available,
    thread_for,
    transition,
)

REMINDERS = {"reminder_24": 24, "reminder_40": 40}
CANDIDATE_REPLIES = {"reply", "offer", "confirmation", "cancellation", "handoff"}


def related(
    session: Session,
    message: ScreeningMessage,
) -> tuple[ScreeningThread, ScreeningRequest]:
    thread = session.get(ScreeningThread, message.thread_id)
    assert thread is not None
    request = session.get(ScreeningRequest, thread.request_id)
    assert request is not None
    return thread, request


def applicant_open(session: Session, request: ScreeningRequest, now: datetime) -> bool:
    app = session.get(Application, request.application_id)
    return bool(app and not app.rejected_at and app.retention_expires_at > now)


class ScreeningJobs:
    def __init__(
        self,
        queue: WorkerQueue,
        mailboxes: MailboxRuntime,
        availability: AvailabilityProvider,
        classifier: ReplyClassifier,
        clock: Callable[[], datetime] = utcnow,
        *,
        drafter: ReplyDrafter | None = None,
    ) -> None:
        self.queue = queue
        self.mailboxes = mailboxes
        self.classifier = classifier
        self.drafter = drafter
        self.clock = clock
        self.conversation = ScreeningConversation(availability)

    @classmethod
    def configured(
        cls, queue: WorkerQueue, mailboxes: MailboxRuntime, client: OpenAI | None
    ) -> "ScreeningJobs | None":
        settings = queue.settings
        if settings.screening_test_tenant_id is None:
            return None
        classifier: ReplyClassifier
        drafter = None
        if client is None:
            classifier = FakeScreeningClassifier()
        else:
            classifier = ScreeningReplyClassifier(
                client,
                settings.grader_model,
                "screening-reply-v1",
                settings.llm_api(),
                settings.llm_output_mode,
                request_tokens=settings.llm_request_tokens,
                output_tokens=min(768, settings.llm_output_tokens),
                chat_disable_thinking=settings.profile_chat_disable_thinking,
            )
            drafter = ScreeningReplyDrafter(
                client,
                settings.grader_model,
                "screening-draft-v1",
                settings.llm_api(),
                settings.llm_output_mode,
                request_tokens=settings.llm_request_tokens,
                output_tokens=min(160, settings.llm_output_tokens),
                chat_disable_thinking=settings.profile_chat_disable_thinking,
            )
        availability = ConfigAvailabilityProvider.load(Path(settings.screening_availability_file))
        return cls(queue, mailboxes, availability, classifier, drafter=drafter)

    def email_provider(self, session: Session, mailbox: Mailbox) -> EmailProvider:
        try:
            credentials = read_credentials(session, self.mailboxes, mailbox)
        except AppError as exc:
            raise ProviderError("SCREENING_MAILBOX_RECONNECT") from exc
        assert mailbox.email
        return self.mailboxes.screening_email(
            mailbox.provider, mailbox.connection_method, mailbox.email, credentials
        )

    def run(self, lease: Lease) -> None:
        if lease.tenant_id != self.queue.settings.screening_test_tenant_id:
            raise ProviderError("SCREENING_TENANT_DISABLED")
        resource_id = lease.aggregate_id
        if lease.kind in {"screening.send", "screening.reply"}:
            with self.queue.owned(lease) as session:
                message = session.get(ScreeningMessage, resource_id)
                if message is None:
                    return
                _, request = related(session, message)
                resource_id = request.id
        with self.queue.serialize_screening(lease, resource_id):
            if lease.kind == "screening.send":
                self.send(lease)
            elif lease.kind == "screening.reply":
                self.reply(lease, resource_id)
            elif lease.kind == "screening.expire":
                self.expire(lease)
            elif lease.kind == "screening.poll":
                self.poll(lease)
            else:
                raise ProviderError("UNKNOWN_JOB")

    def expire(self, lease: Lease) -> None:
        with self.queue.owned(lease) as session:
            request = session.get(ScreeningRequest, lease.aggregate_id)
            if request:
                self.expire_if_due(session, request, self.clock())

    def expire_if_due(self, session: Session, request: ScreeningRequest, now: datetime) -> None:
        if not request.expires_at or request.expires_at > now:
            return
        release_holds(session, request)
        if request.status not in TERMINAL:
            if booking(session, request):
                if request.status == "NEGOTIATING":
                    transition(session, request, "NEEDS_HUMAN", "REPLACEMENT_WINDOW_ENDED")
            else:
                transition(session, request, "NO_RESPONSE", "INVITATION_EXPIRED")

    def poll(self, lease: Lease) -> None:
        with self.queue.owned(lease) as session:
            sources = session.execute(
                select(
                    ScreeningThread.mailbox_connection_id,
                    func.min(ScreeningMessage.last_attempt_at),
                )
                .join(ScreeningMessage, ScreeningMessage.thread_id == ScreeningThread.id)
                .where(
                    ScreeningThread.mailbox_connection_id.is_not(None),
                    ScreeningMessage.last_attempt_at.is_not(None),
                )
                .group_by(ScreeningThread.mailbox_connection_id)
            ).all()
        error = None
        for connection, first_attempt in sources:
            assert connection is not None and first_attempt is not None
            try:
                self.poll_mailbox(lease, connection, first_attempt.replace(microsecond=0))
            except ProviderError as exc:
                error = exc
        if error:
            raise error

    def poll_mailbox(self, lease: Lease, connection: UUID, after: datetime) -> None:
        with self.queue.owned(lease) as session:
            mailbox = session.get(Mailbox, connection)
            if not mailbox or not screening_mailbox_available(self.queue.settings, mailbox):
                return
            email = self.email_provider(session, mailbox)
            generation = mailbox.generation
            saved = session.scalar(
                select(ScreeningInbox).where(ScreeningInbox.mailbox == email.address)
            )
            cursor = (
                saved.cursor if saved and saved.cursor.get("connection") == str(connection) else {}
            )
        incoming, cursor = email.fetch_new(cursor, after)
        with self.queue.owned(lease) as session:
            mailbox = session.scalar(
                select(Mailbox).where(Mailbox.id == connection).with_for_update()
            )
            if (
                not mailbox
                or not screening_mailbox_available(self.queue.settings, mailbox)
                or mailbox.generation != generation
                or mailbox.email != email.address
            ):
                return
            saved = session.scalar(
                select(ScreeningInbox).where(ScreeningInbox.mailbox == email.address)
            )
            if saved is None:
                saved = ScreeningInbox(tenant_id=lease.tenant_id, mailbox=email.address)
                session.add(saved)
            for item in incoming:
                self.import_message(session, connection, email.address, item)
            saved.cursor = {**cursor, "connection": str(connection)}

    def import_message(
        self, session: Session, connection: UUID, address: str, item: IncomingEmail
    ) -> None:
        if (item.automated and not item.bounced_message_ids) or session.scalar(
            select(ScreeningMessage.id).where(
                ScreeningMessage.mailbox == address,
                ScreeningMessage.message_id == item.message_id,
            )
        ):
            return
        ids = (
            list(item.bounced_message_ids)
            if item.bounced_message_ids
            else [
                item.in_reply_to,
                *reversed(item.references),
            ]
        )
        parents = {
            row.message_id: row
            for row in session.scalars(
                select(ScreeningMessage)
                .join(ScreeningThread, ScreeningThread.id == ScreeningMessage.thread_id)
                .where(
                    ScreeningThread.mailbox_connection_id == connection,
                    ScreeningMessage.mailbox == address,
                    ScreeningMessage.message_id.in_(ids),
                    ScreeningMessage.state.in_(
                        ("sent", "sending", "uncertain", "received", "handled")
                    ),
                )
            )
        }
        parent = next((parents[key] for key in ids if key in parents), None)
        if parent is None:
            return
        thread, request = related(session, parent)
        bounce = bool(item.bounced_message_ids)
        reply_address = item.bounced_recipient if bounce else item.sender
        now = self.clock()
        if normalize_email(reply_address or "") != request.candidate_email or not applicant_open(
            session,
            request,
            now,
        ):
            return
        message = ScreeningMessage(
            tenant_id=request.tenant_id,
            thread_id=thread.id,
            mailbox=thread.mailbox,
            created_at=item.received_at or now,
            message_id=item.message_id,
            in_reply_to=item.in_reply_to,
            references=list(item.references),
            generation=parent.generation,
            direction="in",
            kind="bounce" if bounce else "candidate",
            state="handled" if bounce else "received",
            body=item.body,
        )
        session.add(message)
        session.flush()
        if parent.generation != request.generation or request.status in TERMINAL:
            message.state = "handled"
            return
        if bounce:
            release_holds(session, request)
            transition(session, request, "FAILED_DELIVERY", "PERMANENT_BOUNCE")
            return
        if not candidate_reply(message.body):
            message.state = "handled"
            return
        request.replied_at = request.replied_at or now
        emit(
            session,
            request.tenant_id,
            "screening.reply",
            message.id,
            f"screening.reply:{message.id}",
        )

    def draft_reply(self, lease: Lease) -> tuple[str, str] | None:
        if self.drafter is None:
            return None
        with self.queue.owned(lease) as session:
            message = session.get(ScreeningMessage, lease.aggregate_id)
            if (
                not message
                or message.state != "queued"
                or message.kind not in CANDIDATE_REPLIES
                or message.last_attempt_at is not None
            ):
                return None
            thread, request = related(session, message)
            if (
                message.generation != request.generation
                or not message.in_reply_to
                or request.takeover
                or not applicant_open(session, request, self.clock())
            ):
                return None
            inbound = session.scalar(
                select(ScreeningMessage).where(
                    ScreeningMessage.thread_id == thread.id,
                    ScreeningMessage.message_id == message.in_reply_to,
                    ScreeningMessage.generation == message.generation,
                    ScreeningMessage.direction == "in",
                )
            )
            if not inbound:
                return None
            approved = message.body
            latest = candidate_reply(inbound.body)[:4000]
            verified = approved
            if request.callback_number:
                verified = verified.replace(request.callback_number, "[callback number]")
                latest = latest.replace(request.callback_number, "[callback number]")
            latest = latest.replace(request.candidate_email, "[candidate email]")
            context = json.dumps(
                {
                    "candidate_reply": latest,
                    "intent": inbound.intent,
                    "verified_response": verified,
                },
                ensure_ascii=False,
            )
        try:
            opening = acknowledgment(self.drafter.draft(context).acknowledgment)
        except ProviderError:
            return None
        return (approved, f"{opening}\n\n{approved}") if opening else None

    def send(self, lease: Lease) -> None:
        draft = self.draft_reply(lease)
        with self.queue.owned(lease) as session:
            message = session.get(ScreeningMessage, lease.aggregate_id)
            if not message or message.state not in {"queued", "sending"}:
                return
            thread, request = related(session, message)
            if message.state == "sending":
                message.state = "uncertain"
                transition(session, request, "NEEDS_HUMAN", "SMTP_OUTCOME_UNCERTAIN")
                return
            now = self.clock()
            outgoing = self.prepare_send(session, request, thread, message, now)
            if outgoing is None:
                return
            if draft and outgoing.body == draft[0]:
                message.body = draft[1]
                outgoing = replace(outgoing, body=message.body)
            mailbox = session.scalar(
                select(Mailbox).where(Mailbox.id == thread.mailbox_connection_id).with_for_update()
            )
            if (
                not mailbox
                or not screening_mailbox_available(self.queue.settings, mailbox)
                or mailbox.email != thread.mailbox
            ):
                raise ProviderError("SCREENING_MAILBOX_UNAVAILABLE")
            email = self.email_provider(session, mailbox)
            message.state, message.last_attempt_at = "sending", now
        try:
            if outgoing.in_reply_to:
                email.reply_in_thread(outgoing)
            else:
                email.send(outgoing)
        except ProviderError as exc:
            if exc.retryable:
                with self.queue.owned(lease) as session:
                    message = session.get(ScreeningMessage, lease.aggregate_id)
                    if message and message.state == "sending":
                        message.state = "queued"
            raise
        with self.queue.owned(lease) as session:
            message = session.get(ScreeningMessage, lease.aggregate_id)
            if not message or message.state != "sending":
                return
            thread, request = related(session, message)
            message.state, message.sent_at = "sent", self.clock()
            if message.kind == "invite" and request.status == "SHORTLISTED":
                self.start_timers(session, request, thread, message, message.sent_at)
            else:
                actor = "tenant" if message.kind.startswith("manual") else "agent"
                audit(session, request, "EMAIL_SENT", actor)

    def prepare_send(
        self,
        session: Session,
        request: ScreeningRequest,
        thread: ScreeningThread,
        message: ScreeningMessage,
        now: datetime,
    ) -> OutgoingEmail | None:
        kind = message.kind
        self.expire_if_due(session, request, now)
        manual = kind in {"manual", "manual_cancel"}
        cancelling = kind in {"manual_cancel", "cancellation"}
        handoff = kind == "handoff"
        if (
            not request.candidate_email
            or message.generation != request.generation
            or not applicant_open(session, request, now)
            or (request.status in TERMINAL and not (cancelling and request.status == "CANCELLED"))
            or (request.takeover and not manual)
            or (request.status == "NEEDS_HUMAN" and not (manual or handoff))
            or (handoff and request.reason != "CANDIDATE_REQUESTED_RECRUITER")
            or (kind in REMINDERS and (request.replied_at or booking(session, request)))
        ):
            message.state = "skipped"
            return None
        attempts = list(
            session.scalars(
                select(ScreeningMessage.last_attempt_at)
                .where(
                    ScreeningMessage.direction == "out",
                    ScreeningMessage.last_attempt_at > now - timedelta(hours=1),
                )
                .order_by(ScreeningMessage.last_attempt_at)
            )
        )
        if len(attempts) >= self.queue.settings.screening_email_limit_per_hour:
            oldest = attempts[0]
            assert oldest is not None
            retry_at = oldest + timedelta(hours=1)
            emit(
                session,
                request.tenant_id,
                "screening.send",
                message.id,
                f"screening.rate:{message.id}:{retry_at.isoformat()}",
                available_at=retry_at,
            )
            return None
        tenant = session.get(Tenant, request.tenant_id)
        assert tenant is not None
        if kind in {"invite", "manual_offer"} or kind in REMINDERS:
            deadline = request.expires_at or (now + BOOKING_WINDOW)
            keep = []
            if kind in REMINDERS:
                prior = session.scalar(
                    select(ScreeningMessage)
                    .where(
                        ScreeningMessage.thread_id == thread.id,
                        ScreeningMessage.generation == request.generation,
                        ScreeningMessage.kind.in_(("invite", "reminder_24")),
                        ScreeningMessage.state == "sent",
                    )
                    .order_by(ScreeningMessage.sent_at.desc())
                    .limit(1)
                )
                if prior:
                    message.in_reply_to = prior.message_id
                    message.references = [*prior.references, prior.message_id][-100:]
                    keep = [
                        row
                        for slot in prior.offered_slot_ids
                        if (row := session.get(ScreeningSlot, UUID(slot)))
                        and self.conversation.usable(request, tenant, row, now)
                    ]
            rows = self.conversation.new_offers(session, request, tenant, now, deadline, keep)
            if not rows:
                message.state = "skipped"
                transition(session, request, "NEEDS_HUMAN", "NO_AVAILABLE_SLOTS")
                return None
            message.offered_slot_ids = [str(slot.id) for slot in rows]
            if kind == "invite":
                message.body = self.conversation.invitation(session, request, tenant, rows)
            else:
                lead = (
                    "Here are new times for your screening call. Your current booking stays "
                    "reserved until you confirm a new time."
                    if kind == "manual_offer"
                    else f"A reminder about your {request.role_title} screening call."
                )
                message.body = self.conversation.offered_body(request, tenant, rows, lead)
        elif kind == "offer":
            recorded = [session.get(ScreeningSlot, UUID(slot)) for slot in message.offered_slot_ids]
            if not recorded or any(
                row is None or not self.conversation.usable(request, tenant, row, now)
                for row in recorded
            ):
                message.state = "skipped"
                release_holds(session, request)
                transition(session, request, "NEEDS_HUMAN", "OFFER_EXPIRED_BEFORE_SEND")
                return None
        elif kind == "confirmation":
            current = booking(session, request)
            if not current or str(current.id) not in message.offered_slot_ids:
                message.state = "skipped"
                return None
        return OutgoingEmail(
            request.candidate_email,
            f"Re: {thread.subject}" if message.in_reply_to else thread.subject,
            message.body,
            message.message_id,
            message.in_reply_to,
            tuple(message.references),
        )

    def start_timers(
        self,
        session: Session,
        request: ScreeningRequest,
        thread: ScreeningThread,
        message: ScreeningMessage,
        sent_at: datetime,
    ) -> None:
        request.first_sent_at = sent_at
        request.expires_at = sent_at + BOOKING_WINDOW
        session.execute(
            update(ScreeningSlot)
            .where(
                ScreeningSlot.request_id == request.id,
                ScreeningSlot.status == "held",
            )
            .values(hold_until=request.expires_at)
        )
        transition(session, request, "EMAIL_SENT", "INVITATION_SENT")
        transition(session, request, "AWAITING_REPLY", "AWAITING_CANDIDATE")
        for kind, hours in REMINDERS.items():
            queue_email(
                session,
                request,
                thread,
                kind,
                parent=message,
                available_at=sent_at + timedelta(hours=hours),
            )
        emit(
            session,
            request.tenant_id,
            "screening.expire",
            request.id,
            f"screening.expire:{request.id}:{request.generation}",
            available_at=request.expires_at,
        )

    def reply(self, lease: Lease, request_id: UUID) -> None:
        with self.queue.owned(lease) as session:
            request = session.get(ScreeningRequest, request_id)
            if request is None:
                return
            thread = thread_for(session, request)
            # Claim order can differ from IMAP import order when multiple workers are running.
            inbound = session.scalar(
                select(ScreeningMessage)
                .where(
                    ScreeningMessage.thread_id == thread.id,
                    ScreeningMessage.state == "received",
                )
                .order_by(ScreeningMessage.created_at, ScreeningMessage.id)
                .limit(1)
            )
            if inbound is None:
                return
            latest = candidate_reply(inbound.body)
            if not latest:
                inbound.state = "handled"
                return
            now = self.clock()
            self.expire_if_due(session, request, now)
            if (
                request.takeover
                or request.status in TERMINAL
                or request.status == "NEEDS_HUMAN"
                or not applicant_open(session, request, now)
                or not request.expires_at
                or request.expires_at <= now
                or inbound.generation != request.generation
            ):
                inbound.state = "handled"
                return
            if request.auto_replies >= REPLY_LIMIT:
                inbound.state = "handled"
                release_holds(session, request)
                transition(session, request, "NEEDS_HUMAN", "AUTOMATED_REPLY_LIMIT")
                return
            inbound_id, version = inbound.id, request.lock_version
            context = self.reply_context(session, request, thread, inbound, latest)
        try:
            result = self.classifier.interpret(context)
        except ProviderError as exc:
            if exc.retryable and lease.attempts < self.queue.settings.max_job_attempts:
                raise
            with self.queue.owned(lease) as session:
                inbound = session.get(ScreeningMessage, inbound_id)
                request = session.get(ScreeningRequest, request_id)
                if inbound and request and request.lock_version == version:
                    inbound.state = "handled"
                    transition(session, request, "NEEDS_HUMAN", "REPLY_INTERPRETATION_FAILED")
            raise
        with self.queue.owned(lease) as session:
            inbound = session.get(ScreeningMessage, inbound_id)
            request = session.get(ScreeningRequest, request_id)
            if not inbound or not request or inbound.state != "received":
                return
            inbound.state = "handled"
            now = self.clock()
            self.expire_if_due(session, request, now)
            if (
                request.lock_version != version
                or not applicant_open(session, request, now)
                or not request.expires_at
                or request.expires_at <= now
            ):
                return
            self.conversation.act(
                session, request, thread_for(session, request), inbound, result, now
            )

    def reply_context(
        self,
        session: Session,
        request: ScreeningRequest,
        thread: ScreeningThread,
        inbound: ScreeningMessage,
        latest: str,
    ) -> str:
        tenant = session.get(Tenant, request.tenant_id)
        assert tenant is not None
        zone = request.candidate_timezone or self.conversation.availability.timezone(
            tenant.timezone
        )
        current = booking(session, request)
        offer = referenced_offer(session, request, inbound)
        timestamp = func.coalesce(ScreeningMessage.sent_at, ScreeningMessage.created_at)
        history = session.scalars(
            select(ScreeningMessage)
            .where(
                ScreeningMessage.thread_id == thread.id,
                ScreeningMessage.state.in_(("sent", "received", "handled")),
                timestamp <= inbound.created_at,
            )
            .order_by(timestamp, ScreeningMessage.id)
        )
        return json.dumps(
            {
                "thread": [
                    {
                        "direction": row.direction,
                        "body": candidate_reply(row.body) if row.direction == "in" else row.body,
                        "timestamp": (row.sent_at or row.created_at).isoformat(),
                        "generation": row.generation,
                    }
                    for row in history
                    if row.id != inbound.id
                ],
                "latest_reply": latest,
                "received_at": inbound.created_at.isoformat(),
                "generation": inbound.generation,
                "booking_expires_at": request.expires_at.isoformat()
                if request.expires_at
                else None,
                "candidate_timezone": request.candidate_timezone,
                "display_timezone": zone,
                "referenced_offer": offer.body if offer else None,
                "current_booking": show_time(current.starts_at, zone) if current else None,
            },
            ensure_ascii=False,
        )

    def failed(self, lease: Lease, error: ProviderError) -> None:
        if lease.kind != "screening.send" or (
            error.retryable and lease.attempts < self.queue.settings.max_job_attempts
        ):
            return
        with self.queue.owned(lease) as session:
            message = session.get(ScreeningMessage, lease.aggregate_id)
            if message is None or message.state not in {"sending", "queued"}:
                return
            _, request = related(session, message)
            uncertain = error.code == "SCREENING_SEND_UNCERTAIN" or (
                message.state == "sending"
                and error.code in {"PROCESSING_FAILED", "RETRY_EXHAUSTED"}
            )
            message.state = "uncertain" if uncertain else "failed"
            if not uncertain:
                release_holds(session, request)
            transition(
                session,
                request,
                "FAILED_DELIVERY" if error.code == "SCREENING_SMTP_REJECTED" else "NEEDS_HUMAN",
                "SMTP_OUTCOME_UNCERTAIN" if uncertain else error.code,
            )
