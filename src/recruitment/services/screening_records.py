from collections.abc import Callable, Collection
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import (
    Application,
    Candidate,
    Opening,
    Resume,
    ResumeExtraction,
    Tenant,
)
from recruitment.adapters.db.phase2 import MailAttachment, Mailbox, MailMessage
from recruitment.adapters.db.screening import (
    ScreeningEvent,
    ScreeningMessage,
    ScreeningRequest,
    ScreeningSlot,
    ScreeningThread,
)
from recruitment.adapters.mail_runtime import SCREENING_MAILBOX_METHODS
from recruitment.core.config import Settings
from recruitment.domain.profiles import normalize_email, normalize_phone
from recruitment.domain.screening import Slot, known_timezone
from recruitment.services.common import emit


def audit(
    session: Session,
    request: ScreeningRequest,
    reason: str,
    actor: str,
    actor_id: UUID | None = None,
) -> None:
    session.add(
        ScreeningEvent(
            tenant_id=request.tenant_id,
            request_id=request.id,
            status=request.status,
            actor=actor,
            actor_id=actor_id,
            reason=reason,
        )
    )


def transition(
    session: Session,
    request: ScreeningRequest,
    status: str,
    reason: str,
    actor: str = "system",
    actor_id: UUID | None = None,
) -> None:
    request.status, request.reason = status, reason
    request.lock_version += 1
    audit(session, request, reason, actor, actor_id)


def thread_for(session: Session, request: ScreeningRequest) -> ScreeningThread:
    thread = session.scalar(select(ScreeningThread).where(ScreeningThread.request_id == request.id))
    assert thread is not None
    return thread


def booking(session: Session, request: ScreeningRequest) -> ScreeningSlot | None:
    return session.scalar(
        select(ScreeningSlot).where(
            ScreeningSlot.request_id == request.id,
            ScreeningSlot.status == "booked",
        )
    )


def release_holds(
    session: Session,
    request: ScreeningRequest,
    keep: Collection[UUID] = (),
) -> None:
    query = update(ScreeningSlot).where(
        ScreeningSlot.request_id == request.id,
        ScreeningSlot.status == "held",
    )
    if keep:
        query = query.where(ScreeningSlot.id.not_in(keep))
    session.execute(query.values(status="released"))
    if request.pending_slot_id not in keep:
        request.pending_slot_id = None


def slot_range(slot: ScreeningSlot) -> Slot:
    return Slot(slot.starts_at, slot.ends_at, slot.blocked_until)


def occupied(session: Session, now: datetime, skip_holds_for: UUID | None = None) -> list[Slot]:
    session.execute(
        update(ScreeningSlot)
        .where(
            ScreeningSlot.status == "held",
            ScreeningSlot.hold_until <= now,
        )
        .values(status="released")
    )
    query = select(ScreeningSlot).where(
        ScreeningSlot.status.in_(("held", "booked")),
        ScreeningSlot.blocked_until > now,
    )
    if skip_holds_for is not None:
        query = query.where(
            or_(
                ScreeningSlot.request_id != skip_holds_for,
                ScreeningSlot.status == "booked",
            )
        )
    return [slot_range(row) for row in session.scalars(query)]


def select_offers(
    slots: list[Slot],
    blocked: list[Slot],
    timezone: str,
    keep: Collection[Slot] = (),
    *,
    distance: Callable[[Slot], int] | None = None,
    seen: Collection[datetime] = (),
) -> list[Slot]:
    zone = known_timezone(timezone)
    assert zone is not None
    available = [slot for slot in slots if not any(slot.overlaps(reserved) for reserved in blocked)]
    if distance is not None:
        distances = {slot: distance(slot) for slot in available}
        if 0 in distances.values():
            available = [slot for slot in available if distances[slot] == 0]
        available.sort(key=lambda slot: (slot.start in seen, distances[slot], slot.start))
    else:
        available.sort(key=lambda slot: slot.start)
    chosen = list(keep)
    if not chosen and available:
        chosen.append(available[0])
    if chosen and len(chosen) < 3:
        days = {slot.start.astimezone(zone).date() for slot in chosen}
        different_day = next(
            (
                slot
                for slot in available
                if slot.start.astimezone(zone).date() not in days
                and not any(slot.overlaps(saved) for saved in chosen)
            ),
            None,
        )
        if len(days) == 1 and different_day:
            chosen.append(different_day)
    for slot in available:
        if len(chosen) == 3:
            break
        if not any(slot.overlaps(saved) for saved in chosen):
            chosen.append(slot)
    return sorted(chosen, key=lambda slot: slot.start)


def hold(
    session: Session,
    request: ScreeningRequest,
    slot: Slot,
    deadline: datetime,
) -> ScreeningSlot:
    row = ScreeningSlot(
        id=uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        starts_at=slot.start,
        ends_at=slot.end,
        blocked_until=slot.blocked_until,
        hold_until=deadline,
        status="held",
    )
    session.add(row)
    return row


def queue_email(
    session: Session,
    request: ScreeningRequest,
    thread: ScreeningThread,
    kind: str,
    body: str = "",
    *,
    parent: ScreeningMessage | None = None,
    offers: list[ScreeningSlot] | None = None,
    available_at: datetime | None = None,
    auto_reply: bool = False,
) -> ScreeningMessage:
    if auto_reply:
        request.auto_replies += 1
    if kind in {"offer", "confirmation"}:
        session.execute(
            update(ScreeningMessage)
            .where(
                ScreeningMessage.thread_id == thread.id,
                ScreeningMessage.generation == request.generation,
                ScreeningMessage.kind == "offer",
                ScreeningMessage.state == "queued",
            )
            .values(state="skipped")
        )
    if parent is None:
        timestamp = func.coalesce(ScreeningMessage.sent_at, ScreeningMessage.created_at)
        parent = session.scalar(
            select(ScreeningMessage)
            .where(
                ScreeningMessage.thread_id == thread.id,
                ScreeningMessage.state.in_(("sent", "received", "handled", "uncertain")),
            )
            .order_by(timestamp.desc(), ScreeningMessage.id.desc())
            .limit(1)
        )
    row_id = uuid4()
    session.add(
        message := ScreeningMessage(
            id=row_id,
            tenant_id=request.tenant_id,
            thread_id=thread.id,
            mailbox=thread.mailbox,
            message_id=f"<{row_id}@{thread.mailbox.rsplit('@', 1)[-1]}>",
            in_reply_to=parent.message_id if parent else None,
            references=list(dict.fromkeys([*parent.references, parent.message_id]))[-100:]
            if parent
            else [],
            offered_slot_ids=[str(slot.id) for slot in offers or []],
            generation=request.generation,
            direction="out",
            kind=kind,
            state="queued",
            body=body,
        )
    )
    emit(
        session,
        request.tenant_id,
        "screening.send",
        row_id,
        f"screening.send:{row_id}",
        available_at=available_at,
    )
    return message


def screening_mailbox_available(settings: Settings, mailbox: Mailbox) -> bool:
    return bool(
        mailbox.status == "connected"
        and (mailbox.provider, mailbox.connection_method) in SCREENING_MAILBOX_METHODS
        and settings.mailbox_method_enabled(mailbox.connection_method)
        and mailbox.email
    )


def select_mailbox(
    session: Session, settings: Settings, resume: Resume, opening_id: UUID
) -> tuple[Mailbox | None, str]:
    source_connections = (
        select(MailMessage.connection_id)
        .join(MailAttachment, MailAttachment.message_id == MailMessage.id)
        .where(MailAttachment.upload_id == resume.upload_id)
    )
    sources = list(
        session.scalars(
            select(Mailbox)
            .where(Mailbox.tenant_id == resume.tenant_id, Mailbox.id.in_(source_connections))
            .limit(2)
        )
    )
    if len(sources) > 1:
        return None, "SCREENING_MAILBOX_AMBIGUOUS"
    rows = sources or list(
        session.scalars(
            select(Mailbox).where(
                Mailbox.tenant_id == resume.tenant_id, Mailbox.status == "connected"
            )
        )
    )
    eligible = [mailbox for mailbox in rows if screening_mailbox_available(settings, mailbox)]
    if len(eligible) > 1:
        eligible = [mailbox for mailbox in eligible if mailbox.opening_id == opening_id]
        if len(eligible) != 1:
            return None, "SCREENING_MAILBOX_AMBIGUOUS"
    if eligible:
        return eligible[0], "SHORTLIST_RECORDED"
    if any(
        row.status == "connected"
        and (row.provider, row.connection_method) not in SCREENING_MAILBOX_METHODS
        for row in rows
    ):
        return None, "SCREENING_MAILBOX_READ_ONLY"
    return None, "SCREENING_MAILBOX_REQUIRED"


def start_screening(
    session: Session,
    settings: Settings,
    app: Application,
    recruiter_id: UUID,
) -> None:
    if app.tenant_id != settings.screening_test_tenant_id or not app.current_resume_id:
        return
    if session.scalar(
        select(ScreeningRequest.id).where(
            ScreeningRequest.resume_id == app.current_resume_id,
            ScreeningRequest.opening_id == app.opening_id,
        )
    ):
        return
    opening = session.get(Opening, app.opening_id)
    resume = session.get(Resume, app.current_resume_id)
    candidate = session.get(Candidate, app.candidate_id)
    tenant = session.get(Tenant, app.tenant_id)
    if not resume or not opening or opening.status != "active" or not tenant:
        return
    extraction = session.scalar(
        select(ResumeExtraction).where(
            ResumeExtraction.upload_id == resume.upload_id,
        )
    )
    profile = extraction.profile if extraction else {}
    email = normalize_email(str(profile.get("email") or ""))
    mailbox, reason = (
        select_mailbox(session, settings, resume, opening.id)
        if email
        else (None, "PARSED_EMAIL_MISSING")
    )
    status = "SHORTLISTED" if mailbox else "NEEDS_HUMAN" if email else "EMAIL_MISSING"
    request = ScreeningRequest(
        id=uuid4(),
        tenant_id=app.tenant_id,
        application_id=app.id,
        resume_id=resume.id,
        opening_id=opening.id,
        candidate_name=str(
            profile.get("name") or (candidate.profile.get("name") if candidate else "") or ""
        ),
        candidate_email=email,
        callback_number=normalize_phone(str(profile.get("phone") or "")),
        role_title=opening.title,
        status=status,
        reason=reason,
    )
    session.add(request)
    session.flush()
    subject = " ".join(f"Your application at {tenant.name}: screening call".split())
    thread = ScreeningThread(
        id=uuid4(),
        tenant_id=app.tenant_id,
        request_id=request.id,
        mailbox_connection_id=mailbox.id if mailbox else None,
        mailbox=(mailbox.email or "") if mailbox else "",
        subject=subject,
    )
    session.add(thread)
    session.flush()
    audit(session, request, request.reason, "tenant", recruiter_id)
    if mailbox:
        queue_email(session, request, thread, "invite")
