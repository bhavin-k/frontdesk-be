from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import Tenant
from recruitment.adapters.db.screening import (
    ScreeningMessage,
    ScreeningRequest,
    ScreeningSettings,
    ScreeningSlot,
    ScreeningThread,
)
from recruitment.domain.profiles import normalize_phone
from recruitment.domain.screening import (
    MIN_NOTICE,
    ReplyIntent,
    Slot,
    known_timezone,
    preference_distance,
    resolve_time,
)
from recruitment.domain.screening_text import candidate_reply
from recruitment.ports.screening import AvailabilityProvider
from recruitment.services.screening_records import (
    booking,
    hold,
    occupied,
    queue_email,
    release_holds,
    select_offers,
    slot_range,
    transition,
)


def has_evidence(reply: str, evidence: str) -> bool:
    return bool(evidence.strip()) and (
        " ".join(evidence.casefold().split()) in " ".join(reply.casefold().split())
    )


def referenced_offer(
    session: Session,
    request: ScreeningRequest,
    message: ScreeningMessage,
) -> ScreeningMessage | None:
    references = [message.in_reply_to, *reversed(message.references)]
    offers = {
        row.message_id: row
        for row in session.scalars(
            select(ScreeningMessage).where(
                ScreeningMessage.thread_id == message.thread_id,
                ScreeningMessage.message_id.in_(references),
                ScreeningMessage.generation == request.generation,
                ScreeningMessage.direction == "out",
                ScreeningMessage.state == "sent",
                ScreeningMessage.kind.in_(
                    ("invite", "reminder_24", "reminder_40", "offer", "manual_offer")
                ),
            )
        )
        if row.offered_slot_ids
    }
    return next((offers[ref] for ref in references if ref in offers), None)


def show_time(value: datetime, timezone: str) -> str:
    zone = known_timezone(timezone)
    assert zone is not None
    return value.astimezone(zone).strftime("%a %d %b %Y, %I:%M %p") + f" ({timezone})"


def duration(slot: ScreeningSlot) -> int:
    return int((slot.ends_at - slot.starts_at).total_seconds() // 60)


class ScreeningConversation:
    def __init__(self, availability: AvailabilityProvider) -> None:
        self.availability = availability

    def new_offers(
        self,
        session: Session,
        request: ScreeningRequest,
        tenant: Tenant,
        now: datetime,
        deadline: datetime,
        keep: list[ScreeningSlot] | None = None,
        *,
        distance: Callable[[Slot], int] | None = None,
        seen: set[datetime] | None = None,
        eligible: Callable[[Slot], bool] | None = None,
    ) -> list[ScreeningSlot]:
        if deadline < now + MIN_NOTICE:
            release_holds(session, request)
            return []
        blocked = occupied(session, now, request.id)
        candidates = self.availability.slots(now + MIN_NOTICE, deadline, tenant.timezone)
        timezone = request.candidate_timezone or self.availability.timezone(tenant.timezone)
        if eligible is not None:
            candidates = [slot for slot in candidates if eligible(slot)]
        retained = {
            slot_range(row): row
            for row in keep or []
            if eligible is None or eligible(slot_range(row))
        }
        selected = select_offers(
            candidates,
            blocked,
            timezone,
            retained,
            distance=distance,
            seen=seen or (),
        )
        if distance is not None:
            retained = {
                slot_range(row): row
                for row in session.scalars(
                    select(ScreeningSlot).where(
                        ScreeningSlot.request_id == request.id, ScreeningSlot.status == "held"
                    )
                )
                if slot_range(row) in selected and row.hold_until and row.hold_until > now
            }
        release_holds(session, request, keep=[row.id for row in retained.values()])
        return [
            retained[slot] if slot in retained else hold(session, request, slot, deadline)
            for slot in selected
        ]

    def counter_offer(
        self,
        session: Session,
        request: ScreeningRequest,
        tenant: Tenant,
        thread: ScreeningThread,
        inbound: ScreeningMessage,
        intent: ReplyIntent,
        now: datetime,
        *,
        current: ScreeningSlot | None,
        target: datetime | None = None,
    ) -> None:
        deadline = request.expires_at
        assert deadline is not None
        zone = known_timezone(
            request.candidate_timezone or self.availability.timezone(tenant.timezone)
        )
        assert zone is not None
        offers = [
            row
            for row in session.scalars(
                select(ScreeningMessage)
                .where(
                    ScreeningMessage.thread_id == thread.id,
                    ScreeningMessage.direction == "out",
                    ScreeningMessage.state == "sent",
                    ScreeningMessage.generation == request.generation,
                )
                .order_by(ScreeningMessage.sent_at.desc())
            )
            if row.offered_slot_ids
        ]
        starts = {
            str(slot.id): slot.starts_at
            for slot in session.scalars(
                select(ScreeningSlot).where(ScreeningSlot.request_id == request.id)
            )
        }
        seen = {starts[key] for offer in offers for key in offer.offered_slot_ids if key in starts}
        referenced = referenced_offer(session, request, inbound) or (offers[0] if offers else None)
        offered_local = [
            starts[key].astimezone(zone)
            for key in (referenced.offered_slot_ids if referenced else [])
            if key in starts
        ]
        if (
            target is None
            and intent.intent == "ACCEPT_SLOT"
            and intent.slot_number
            and intent.slot_number <= len(offered_local)
        ):
            target = offered_local[intent.slot_number - 1]

        def distance(slot: Slot) -> int:
            if target is not None:
                return int(abs((slot.start - target).total_seconds()))
            if intent.preference is not None:
                return preference_distance(
                    slot.start.astimezone(zone), intent.preference, offered_local
                )
            return 0

        def eligible(slot: Slot) -> bool:
            if not intent.permits(slot, zone):
                return False
            preference = intent.preference
            if preference and preference.required:
                first = slot.start.astimezone(zone)
                last = (slot.end - timedelta(microseconds=1)).astimezone(zone)
                return all(
                    preference_distance(instant, preference, offered_local) == 0
                    for instant in (first, last)
                )
            return True

        rows = self.new_offers(
            session,
            request,
            tenant,
            now,
            deadline,
            distance=distance,
            seen=seen,
            eligible=eligible,
        )
        if not rows:
            transition(session, request, "NEEDS_HUMAN", "NO_AVAILABLE_SLOTS", "agent")
            return
        if intent.intent == "ACCEPT_SLOT":
            lead = "That offer has changed. Here are the times we can hold for you now."
        elif target is not None:
            lead = "That time is unavailable. Here are the closest times we can offer."
        elif any(distance(slot_range(row)) == 0 for row in rows):
            lead = (
                "Here are times on the other days."
                if intent.unavailable_dates and not intent.unavailable_windows
                else "Here are the times we can offer."
            )
        else:
            period = intent.preference.period if intent.preference else None
            if period == "evening":
                window = (
                    "on that date"
                    if intent.preference and intent.preference.date
                    else "in this window"
                )
                lead = (
                    f"We don’t have an evening slot {window}. "
                    "Here are the closest times we can offer."
                )
            else:
                lead = (
                    "We don’t have an opening in that time range. "
                    "Here are the closest times we can offer."
                )
        if current:
            lead += " Your current booking stays reserved until you confirm a new time."
        transition(session, request, "NEGOTIATING", "NEW_TIMES_OFFERED", "agent")
        queue_email(
            session,
            request,
            thread,
            "offer",
            self.offered_body(request, tenant, rows, lead),
            parent=inbound,
            offers=rows,
            auto_reply=True,
        )

    def usable(
        self,
        request: ScreeningRequest,
        tenant: Tenant,
        row: ScreeningSlot,
        now: datetime,
    ) -> bool:
        return bool(
            request.expires_at
            and now + MIN_NOTICE <= row.starts_at <= request.expires_at
            and row.status == "held"
            and row.hold_until
            and row.hold_until > now
            and self.availability.validate(row.starts_at, tenant.timezone) == slot_range(row)
        )

    def offered_body(
        self,
        request: ScreeningRequest,
        tenant: Tenant,
        rows: list[ScreeningSlot],
        prefix: str,
    ) -> str:
        zone = request.candidate_timezone or self.availability.timezone(tenant.timezone)
        times = "\n".join(
            f"{index}. {show_time(row.starts_at, zone)}" for index, row in enumerate(rows, 1)
        )
        return (
            f"{prefix}\n\n{tenant.name}'s hiring team will call you for about "
            f"{duration(rows[0])} minutes.\n\n{times}\n\n"
            "Please choose one of these times or suggest another time and include your timezone."
        )

    def invitation(
        self,
        session: Session,
        request: ScreeningRequest,
        tenant: Tenant,
        rows: list[ScreeningSlot],
    ) -> str:
        settings = session.scalar(select(ScreeningSettings))
        first_name = request.candidate_name.split(maxsplit=1)[0] if request.candidate_name else ""
        greeting = f"Hi {first_name}," if first_name else "Hello,"
        note = f"\n\n{settings.invitation_note}" if settings and settings.invitation_note else ""
        signature = (
            settings.signature
            if settings and settings.signature
            else (f"{tenant.name} Hiring Team")
        )
        body = self.offered_body(
            request,
            tenant,
            rows,
            (
                f"{greeting}\n\n{tenant.name} shortlisted your CV for {request.role_title} and "
                f"would like to invite you to a phone screening.{note}"
            ),
        )
        phone = (
            f"Please confirm we can call you at {request.callback_number}."
            if request.callback_number
            else "Please include a phone number we can call."
        )
        return (
            f"{body}\n{phone}\nWe aim to start the call within 48 hours of this email.\n\n"
            f"Reply ‘not interested’ to stop messages about this screening.\n\n{signature}"
        )

    def find_selection(
        self,
        session: Session,
        request: ScreeningRequest,
        tenant: Tenant,
        inbound: ScreeningMessage,
        intent: ReplyIntent,
        now: datetime,
    ) -> ScreeningSlot | None:
        offer = referenced_offer(session, request, inbound)
        if intent.intent == "ACCEPT_SLOT" and not offer:
            return None
        if intent.intent == "ACCEPT_SLOT" and intent.slot_number and offer:
            ids = offer.offered_slot_ids
            if intent.slot_number <= len(ids):
                row = session.get(ScreeningSlot, UUID(ids[intent.slot_number - 1]))
                if row and row.request_id == request.id and self.usable(request, tenant, row, now):
                    return row
            return None
        if intent.intent == "ACCEPT_SLOT" and offer:
            zone = request.candidate_timezone or self.availability.timezone(tenant.timezone)
            proposed = resolve_time(intent.proposed_datetime, zone)
            for identifier in offer.offered_slot_ids:
                row = session.get(ScreeningSlot, UUID(identifier))
                if (
                    row
                    and row.request_id == request.id
                    and row.starts_at == proposed
                    and self.usable(request, tenant, row, now)
                ):
                    return row
            return None
        if intent.intent == "PROVIDE_DETAILS" and request.pending_slot_id:
            row = session.get(ScreeningSlot, request.pending_slot_id)
            if row and row.request_id == request.id and self.usable(request, tenant, row, now):
                return row
        if intent.intent not in {"PROPOSE_TIME", "RESCHEDULE"}:
            return None
        start = resolve_time(intent.proposed_datetime, request.candidate_timezone)
        deadline = request.expires_at
        if not start or not deadline or not now + MIN_NOTICE <= start <= deadline:
            return None
        slot = self.availability.validate(start, tenant.timezone)
        if not slot or any(slot.overlaps(part) for part in occupied(session, now, request.id)):
            return None
        saved = session.scalar(
            select(ScreeningSlot).where(
                ScreeningSlot.request_id == request.id,
                ScreeningSlot.status == "held",
                ScreeningSlot.starts_at == start,
            )
        )
        if saved and self.usable(request, tenant, saved, now):
            return saved
        release_holds(session, request)
        return hold(session, request, slot, deadline)

    def book(
        self,
        session: Session,
        request: ScreeningRequest,
        tenant: Tenant,
        thread: ScreeningThread,
        inbound: ScreeningMessage,
        row: ScreeningSlot,
        existing: ScreeningSlot | None,
        now: datetime,
    ) -> None:
        # The worker holds the tenant advisory lock for validation and the swap.
        if any(slot_range(row).overlaps(part) for part in occupied(session, now, request.id)):
            transition(session, request, "NEEDS_HUMAN", "BOOKING_CONFLICT", "agent")
            return
        if existing:
            existing.status = "released"
        release_holds(session, request, keep=[row.id])
        session.flush()
        row.status, row.hold_until = "booked", None
        request.pending_slot_id = None
        transition(
            session, request, "BOOKED", "CALL_RESCHEDULED" if existing else "CALL_BOOKED", "agent"
        )
        zone = request.candidate_timezone or self.availability.timezone(tenant.timezone)
        text = (
            f"Your {request.role_title} phone screening with {tenant.name} is confirmed for "
            f"{show_time(row.starts_at, zone)}. {tenant.name}'s hiring team will call "
            f"{request.callback_number}. Please allow {duration(row)} minutes."
        )
        queue_email(
            session,
            request,
            thread,
            "confirmation",
            text,
            parent=inbound,
            offers=[row],
            auto_reply=True,
        )

    def act(
        self,
        session: Session,
        request: ScreeningRequest,
        thread: ScreeningThread,
        inbound: ScreeningMessage,
        intent: ReplyIntent,
        now: datetime,
    ) -> None:
        tenant = session.get(Tenant, request.tenant_id)
        assert tenant is not None
        latest = candidate_reply(inbound.body)
        kind = intent.intent if has_evidence(latest, intent.evidence) else "UNCLEAR"
        if latest.casefold().strip(" .!") in {"not interested", "unsubscribe", "stop"}:
            kind = "DECLINE"
        inbound.intent = kind
        if kind == "ACKNOWLEDGE":
            return
        if kind == "HANDOFF":
            release_holds(session, request)
            transition(session, request, "NEEDS_HUMAN", "CANDIDATE_REQUESTED_RECRUITER", "agent")
            queue_email(
                session,
                request,
                thread,
                "handoff",
                "I’ve passed your message to the hiring team for review.",
                parent=inbound,
                auto_reply=True,
            )
            return
        current = booking(session, request)
        if kind == "DECLINE" or (kind == "CANCEL" and current):
            release_holds(session, request)
            if current and current.starts_at > now:
                current.status = "released"
            status = "DECLINED" if kind == "DECLINE" else "CANCELLED"
            transition(session, request, status, "CANDIDATE_" + status, "agent")
            if status == "CANCELLED":
                queue_email(
                    session,
                    request,
                    thread,
                    "cancellation",
                    "Your screening call has been cancelled.",
                    parent=inbound,
                    auto_reply=True,
                )
            return
        if kind != "UNCLEAR" and known_timezone(intent.timezone):
            request.candidate_timezone = intent.timezone
        if kind != "UNCLEAR" and intent.phone and has_evidence(latest, intent.phone):
            phone = normalize_phone(intent.phone)
            if phone:
                request.callback_number = phone
        request.lock_version += 1
        if kind == "REQUEST_OPTIONS" or (kind == "QUESTION" and intent.question == "slots"):
            if current and current.starts_at <= now:
                transition(session, request, "NEEDS_HUMAN", "CALL_ALREADY_STARTED", "agent")
            else:
                self.counter_offer(
                    session, request, tenant, thread, inbound, intent, now, current=current
                )
            return
        if kind == "QUESTION":
            call = current or session.scalar(
                select(ScreeningSlot)
                .where(
                    ScreeningSlot.request_id == request.id,
                    ScreeningSlot.status == "held",
                    ScreeningSlot.hold_until > now,
                )
                .order_by(ScreeningSlot.starts_at)
                .limit(1)
            )
            answer = self.answer(tenant, request, call, intent.question)
            queue_email(session, request, thread, "reply", answer, parent=inbound, auto_reply=True)
            return
        if (
            current
            and request.status == "BOOKED"
            and kind
            in {
                "ACCEPT_SLOT",
                "PROVIDE_DETAILS",
                "UNCLEAR",
            }
        ):
            zone = request.candidate_timezone or self.availability.timezone(tenant.timezone)
            body = (
                f"Your screening call is booked for {show_time(current.starts_at, zone)}. "
                f"Our hiring team will call {request.callback_number}."
            )
            queue_email(session, request, thread, "reply", body, parent=inbound, auto_reply=True)
            return
        clarification = (
            "Which of the offered times works for you? You can also suggest another time."
        )
        if kind in {"ACCEPT_SLOT", "PROPOSE_TIME", "PROVIDE_DETAILS", "RESCHEDULE"}:
            if current and current.starts_at <= now:
                transition(session, request, "NEEDS_HUMAN", "CALL_ALREADY_STARTED", "agent")
                return
            detail = intent.missing_detail if kind in {"PROPOSE_TIME", "RESCHEDULE"} else None
            row = (
                None
                if detail
                else self.find_selection(session, request, tenant, inbound, intent, now)
            )
            if row:
                candidate_zone = known_timezone(
                    request.candidate_timezone or self.availability.timezone(tenant.timezone)
                )
                assert candidate_zone is not None
                if not intent.permits(slot_range(row), candidate_zone):
                    self.counter_offer(
                        session, request, tenant, thread, inbound, intent, now, current=current
                    )
                    return
                if current and kind in {"RESCHEDULE", "PROPOSE_TIME"}:
                    release_holds(session, request, keep=[row.id])
                    transition(session, request, "NEGOTIATING", "REPLACEMENT_OFFERED", "agent")
                    body = self.offered_body(
                        request,
                        tenant,
                        [row],
                        (
                            "We can offer this replacement. Your current booking stays "
                            "reserved until you confirm the new time."
                        ),
                    )
                    queue_email(
                        session,
                        request,
                        thread,
                        "offer",
                        body,
                        parent=inbound,
                        offers=[row],
                        auto_reply=True,
                    )
                elif not request.callback_number:
                    request.pending_slot_id = row.id
                    transition(session, request, "NEGOTIATING", "CALLBACK_REQUIRED", "agent")
                    queue_email(
                        session,
                        request,
                        thread,
                        "reply",
                        "Please send the phone number our hiring team can call.",
                        parent=inbound,
                        auto_reply=True,
                    )
                else:
                    self.book(session, request, tenant, thread, inbound, row, current, now)
                return
            proposal_zone = request.candidate_timezone
            if kind == "ACCEPT_SLOT":
                proposal_zone = proposal_zone or self.availability.timezone(tenant.timezone)
            target = resolve_time(intent.proposed_datetime, proposal_zone)
            if not detail and (
                kind == "ACCEPT_SLOT"
                or (kind in {"PROPOSE_TIME", "RESCHEDULE"} and target)
                or (kind == "RESCHEDULE" and not intent.proposed_datetime)
            ):
                self.counter_offer(
                    session,
                    request,
                    tenant,
                    thread,
                    inbound,
                    intent,
                    now,
                    current=current,
                    target=target,
                )
                return
            if kind in {"PROPOSE_TIME", "RESCHEDULE"}:
                detail = detail or ("timezone" if intent.proposed_datetime else "date")
                clarification = {
                    "date": "What date did you have in mind?",
                    "time": "What time would work for you that day?",
                    "timezone": "Which timezone should I use?",
                }[detail]
        transition(session, request, "NEGOTIATING", "CLARIFICATION_REQUESTED", "agent")
        queue_email(
            session,
            request,
            thread,
            "reply",
            clarification,
            parent=inbound,
            auto_reply=True,
        )

    def answer(
        self,
        tenant: Tenant,
        request: ScreeningRequest,
        current: ScreeningSlot | None,
        question: str | None,
    ) -> str:
        if question == "caller":
            return f"{tenant.name}'s hiring team will call you for the screening."
        if question == "duration" and current:
            return f"Please allow {duration(current)} minutes for the call."
        if question == "phone" and request.callback_number:
            return f"Our hiring team will call {request.callback_number}."
        if question == "duration":
            return "I can confirm the call length when we find a time that works for you."
        if question == "phone":
            return "Please send a phone number that our hiring team can call."
        return (
            "This thread is for arranging your screening call. You can choose one of the offered "
            "times or suggest a date, time and timezone."
        )
