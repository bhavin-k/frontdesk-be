from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import Application
from recruitment.adapters.db.screening import (
    ScreeningEvent,
    ScreeningMessage,
    ScreeningRequest,
    ScreeningSettings,
    ScreeningSlot,
)
from recruitment.core.config import Settings
from recruitment.core.errors import AppError
from recruitment.core.time import utcnow
from recruitment.domain.screening import MIN_NOTICE, TERMINAL
from recruitment.domain.screening_text import candidate_reply
from recruitment.services.auth import Principal
from recruitment.services.common import advisory_lock, existing_request, missing, remember_request
from recruitment.services.screening_records import (
    booking,
    queue_email,
    release_holds,
    thread_for,
    transition,
)
from recruitment.services.tenancy import TenancyService

ScreeningAction = Literal["take_over", "reply", "cancel", "reschedule", "resend", "close"]


class ScreeningActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: ScreeningAction
    expected_version: int = Field(ge=1, strict=True)
    body: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_body(self) -> "ScreeningActionInput":
        if self.action == "reply" and not self.body:
            raise ValueError("Enter a reply.")
        if self.action != "reply" and self.body is not None:
            raise ValueError("Only replies have a message body.")
        return self


class InvitationCopy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    invitation_note: str = Field(max_length=1000)
    signature: str = Field(max_length=300)


def actions_for(
    request: ScreeningRequest,
    current: ScreeningSlot | None,
    now: datetime,
) -> list[ScreeningAction]:
    actions: list[ScreeningAction] = []
    active = request.status not in TERMINAL
    live = bool(request.expires_at and request.expires_at > now)
    if active and request.first_sent_at and live:
        actions.append("reply" if request.takeover else "take_over")
    elif request.status == "NEEDS_HUMAN" and not request.takeover:
        actions.append("take_over")
    if current and current.starts_at > now:
        actions.append("cancel")
        if active and request.expires_at and now + MIN_NOTICE <= request.expires_at:
            actions.append("reschedule")
    elif not current and request.status != "CLOSED":
        actions.append("close")
    if request.status == "NO_RESPONSE":
        actions.append("resend")
    return actions


def summary(request: ScreeningRequest, current: ScreeningSlot | None) -> dict[str, Any]:
    return {
        "id": request.id,
        "candidate_name": request.candidate_name,
        "role_title": request.role_title,
        "status": request.status,
        "reason": request.reason,
        "created_at": request.created_at,
        "first_sent_at": request.first_sent_at,
        "expires_at": request.expires_at,
        "booking": {"starts_at": current.starts_at, "ends_at": current.ends_at}
        if current
        else None,
    }


class ScreeningDashboard:
    def __init__(self, tenants: TenancyService, settings: Settings) -> None:
        self.tenants, self.settings = tenants, settings

    def list(
        self,
        actor: Principal,
        tenant_id: UUID,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant_id) as session:
            rows = session.execute(
                select(ScreeningRequest, ScreeningSlot)
                .join(Application, Application.id == ScreeningRequest.application_id)
                .outerjoin(
                    ScreeningSlot,
                    and_(
                        ScreeningSlot.request_id == ScreeningRequest.id,
                        ScreeningSlot.status == "booked",
                    ),
                )
                .where(Application.retention_expires_at > utcnow())
                .order_by(ScreeningRequest.created_at.desc(), ScreeningRequest.id)
                .limit(limit + 1)
                .offset(offset)
            ).all()
            return {
                "enabled": tenant_id == self.settings.screening_test_tenant_id,
                "items": [summary(request, slot) for request, slot in rows[:limit]],
                "has_more": len(rows) > limit,
            }

    def request_for(self, session: Session, request_id: UUID) -> ScreeningRequest:
        request = session.scalar(
            select(ScreeningRequest)
            .join(Application, Application.id == ScreeningRequest.application_id)
            .where(ScreeningRequest.id == request_id, Application.retention_expires_at > utcnow())
        )
        if request is None:
            raise missing()
        return request

    def detail(self, actor: Principal, tenant_id: UUID, request_id: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant_id) as session:
            request = self.request_for(session, request_id)
            current = booking(session, request)
            thread = thread_for(session, request)
            messages = []
            for row in session.scalars(
                select(ScreeningMessage)
                .where(
                    ScreeningMessage.thread_id == thread.id,
                    ScreeningMessage.body != "",
                    ScreeningMessage.state != "skipped",
                )
                .order_by(
                    func.coalesce(ScreeningMessage.sent_at, ScreeningMessage.created_at),
                    ScreeningMessage.id,
                )
            ):
                body = candidate_reply(row.body) if row.direction == "in" else row.body
                if body:
                    messages.append(
                        {
                            "id": row.id,
                            "direction": row.direction,
                            "state": row.state,
                            "body": body,
                            "created_at": row.sent_at or row.created_at,
                        }
                    )
            return {
                **summary(request, current),
                "subject": thread.subject,
                "sender_email": thread.mailbox or None,
                "candidate_email": request.candidate_email,
                "callback_number": request.callback_number,
                "version": request.lock_version,
                "takeover": request.takeover,
                "actions": actions_for(request, current, utcnow())
                if tenant_id == self.settings.screening_test_tenant_id
                else [],
                "messages": messages,
                "events": [
                    {
                        "id": event.id,
                        "status": event.status,
                        "actor": event.actor,
                        "reason": event.reason,
                        "created_at": event.created_at,
                    }
                    for event in session.scalars(
                        select(ScreeningEvent)
                        .where(
                            ScreeningEvent.request_id == request.id,
                        )
                        .order_by(ScreeningEvent.created_at, ScreeningEvent.id)
                    )
                ],
            }

    def edit_copy(
        self,
        actor: Principal,
        tenant_id: UUID,
        changes: InvitationCopy | None = None,
    ) -> InvitationCopy:
        with self.tenants.scope(actor, tenant_id) as session:
            if changes:
                if tenant_id != self.settings.screening_test_tenant_id:
                    raise AppError("SCREENING_DISABLED", "The screening pilot is disabled.", 409)
                advisory_lock(session, f"tenant:{tenant_id}")
            row = session.scalar(select(ScreeningSettings))
            if changes:
                if row is None:
                    row = ScreeningSettings(tenant_id=tenant_id)
                    session.add(row)
                row.invitation_note, row.signature = changes.invitation_note, changes.signature
            return InvitationCopy(
                invitation_note=row.invitation_note if row else "",
                signature=row.signature if row else "",
            )

    def act(
        self,
        actor: Principal,
        tenant_id: UUID,
        request_id: UUID,
        command: ScreeningActionInput,
        key: str,
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant_id) as session:
            advisory_lock(session, f"screening:{tenant_id}:{request_id}")
            advisory_lock(session, f"tenant:{tenant_id}")
            request = self.request_for(session, request_id)
            if tenant_id != self.settings.screening_test_tenant_id:
                raise AppError("SCREENING_DISABLED", "The screening pilot is disabled.", 409)
            payload = {"request_id": request_id, **command.model_dump()}
            if existing_request(session, tenant_id, "screening-action", key, payload):
                return {"id": request.id, "version": request.lock_version}
            current, now = booking(session, request), utcnow()
            if request.lock_version != command.expected_version:
                raise AppError(
                    "STALE_SCREENING", "Refresh the screening request before acting.", 409
                )
            if command.action not in actions_for(request, current, now):
                raise AppError("SCREENING_ACTION_UNAVAILABLE", "This action is unavailable.", 409)
            thread = thread_for(session, request)
            status = request.status
            if command.action == "take_over":
                request.takeover = True
            elif command.action == "reply":
                assert command.body is not None
                queue_email(session, request, thread, "manual", command.body)
            elif command.action == "cancel":
                assert current is not None
                current.status, status = "released", "CANCELLED"
                release_holds(session, request)
                queue_email(
                    session,
                    request,
                    thread,
                    "manual_cancel",
                    "Your screening call has been cancelled by the hiring team.",
                )
            elif command.action == "reschedule":
                release_holds(session, request)
                request.takeover = False
                status = "NEGOTIATING"
                queue_email(session, request, thread, "manual_offer")
            elif command.action == "resend":
                release_holds(session, request)
                request.generation += 1
                request.first_sent_at = request.expires_at = request.replied_at = None
                request.takeover = False
                status = "SHORTLISTED"
                queue_email(session, request, thread, "invite")
            elif command.action == "close":
                release_holds(session, request)
                status = "CLOSED"
            transition(
                session,
                request,
                status,
                f"RECRUITER_{command.action.upper()}",
                "tenant",
                actor.user_id,
            )
            remember_request(session, tenant_id, "screening-action", key, payload, request.id)
            return {"id": request.id, "version": request.lock_version}
