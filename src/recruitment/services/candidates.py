from typing import Any
from uuid import UUID, uuid4

import phonenumbers
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import JSON, String, and_, case, cast, column, delete, func, or_, select, true
from sqlalchemy.orm import Session, aliased, load_only

from recruitment.adapters.db.matching import MatchingEvaluation, RoleAssessment
from recruitment.adapters.db.models import (
    Application,
    Candidate,
    Contact,
    CVUpload,
    MatchRun,
    Opening,
    Resume,
    ResumeExtraction,
    ResumeFile,
)
from recruitment.adapters.db.phase2 import (
    ContactRevision,
    IdentityReview,
    MailAttachment,
    Mailbox,
    MailMessage,
)
from recruitment.core.errors import AppError
from recruitment.core.time import utcnow
from recruitment.domain.profiles import clean, with_contacts
from recruitment.services.auth import Principal
from recruitment.services.common import (
    advisory_lock,
    emit,
    existing_request,
    missing,
    remember_request,
)
from recruitment.services.evaluation_reads import assessment_read
from recruitment.services.tenancy import TenancyService

NO_MATCH_SCORE_CUTOFF = 15


def contact_values(values: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in values.items() if key in {"name", "email", "mobile"}}
    try:
        for key, value in result.items():
            if value is None or not value.strip():
                result[key] = None
            elif key == "name":
                result[key] = clean(value)
            elif key == "email":
                result[key] = validate_email(
                    value, check_deliverability=False
                ).normalized.casefold()
            else:
                phone = phonenumbers.parse(value, "IN")
                if not phonenumbers.is_valid_number(phone):
                    raise ValueError
                result[key] = phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)
    except (EmailNotValidError, phonenumbers.NumberParseException, ValueError) as exc:
        raise AppError(
            "INVALID_CONTACT", "Enter a valid candidate email or mobile number.", 422
        ) from exc
    return result


def correct_contacts(
    session: Session,
    actor: Principal,
    candidate: Candidate,
    values: dict[str, Any],
    expected: int,
    reason: str,
) -> None:
    if candidate.contact_revision != expected:
        raise AppError("STALE_CONTACT_REVISION", "Refresh the candidate before editing.", 409)
    values = contact_values(values)
    if not values:
        raise AppError("INVALID_CONTACT", "Choose a contact field to update.", 422)
    for kind in ("email", "mobile"):
        if kind not in values:
            continue
        value = values[kind]
        owner = (
            session.scalar(
                select(Contact.candidate_id).where(Contact.kind == kind, Contact.value == value)
            )
            if value
            else None
        )
        if owner and owner != candidate.id:
            raise AppError(
                "CONTACT_CONFLICT",
                "Another candidate owns this contact. Review the identity first.",
                409,
            )
        session.execute(
            delete(Contact).where(Contact.candidate_id == candidate.id, Contact.kind == kind)
        )
        if value:
            session.add(
                Contact(
                    tenant_id=candidate.tenant_id, candidate_id=candidate.id, kind=kind, value=value
                )
            )
    before = {
        key: candidate.profile.get("phone", candidate.profile.get("mobile"))
        if key == "mobile"
        else candidate.profile.get(key)
        for key in values
    }
    candidate.profile = with_contacts(candidate.profile, values)
    candidate.contact_overrides = {**candidate.contact_overrides, **values}
    candidate.contact_revision += 1
    session.add(
        ContactRevision(
            tenant_id=candidate.tenant_id,
            candidate_id=candidate.id,
            revision=candidate.contact_revision,
            actor_id=actor.user_id,
            before=before,
            after=values,
            reason=reason,
        )
    )


def no_opening_matches(session: Session, best: RoleAssessment) -> bool:
    if (
        best.recommendation != "recommended_rejection"
        or best.score is None
        or best.score >= NO_MATCH_SCORE_CUTOFF
    ):
        return False
    return (
        session.scalar(
            select(RoleAssessment.id)
            .where(
                RoleAssessment.evaluation_id == best.evaluation_id,
                or_(
                    RoleAssessment.state != "completed",
                    RoleAssessment.recommendation != "recommended_rejection",
                    RoleAssessment.provisional.is_(True),
                    RoleAssessment.score.is_(None),
                    RoleAssessment.score >= NO_MATCH_SCORE_CUTOFF,
                ),
            )
            .limit(1)
        )
        is None
    )


def candidate_read(candidate: Candidate, session: Session | None = None) -> dict[str, Any]:
    result = {
        "id": candidate.id,
        "profile": candidate.profile,
        "contact_revision": candidate.contact_revision,
        "current_source_at": candidate.current_source_at,
    }
    if session is not None:
        app = session.scalar(
            select(Application)
            .where(Application.candidate_id == candidate.id)
            .order_by(Application.created_at.desc(), Application.id.desc())
        )
        opening_id = app.opening_id if app else None
        opening_title = None
        no_matching_opening = False
        run = session.get(MatchRun, app.latest_match_id) if app and app.latest_match_id else None
        score = run.score if app and run and run.resume_id == app.current_resume_id else None
        if app and app.latest_evaluation_id:
            evaluation = session.get(MatchingEvaluation, app.latest_evaluation_id)
            if (
                evaluation
                and evaluation.resume_id == app.current_resume_id
                and evaluation.state == "completed"
            ):
                roles = select(RoleAssessment).where(RoleAssessment.evaluation_id == evaluation.id)
                role = session.scalar(
                    roles.where(
                        RoleAssessment.opening_id == app.opening_id
                        if app.status == "shortlisted"
                        else RoleAssessment.id == evaluation.recommended_assessment_id
                    )
                )
                if role is not None:
                    score = role.score
                    if app.status != "shortlisted":
                        no_matching_opening = no_opening_matches(session, role)
                        opening_id = None if no_matching_opening else role.opening_id
                        if opening_id is not None:
                            opening_title = assessment_read(session, role, evaluation)[
                                "opening_title"
                            ]
        if opening_title is None and opening_id is not None:
            opening = session.get(Opening, opening_id)
            opening_title = opening.title if opening else None
        resume = (
            session.get(Resume, app.current_resume_id) if app and app.current_resume_id else None
        )
        result.update(
            application_id=app.id if app else None,
            status=app.status if app else "processing",
            opening_id=opening_id,
            opening_title=opening_title,
            score=score,
            no_matching_opening=no_matching_opening,
            latest_upload_id=resume.upload_id if resume else None,
        )
    return result


def candidate_page(session: Session, limit: int, offset: int) -> list[dict[str, Any]]:
    page = (
        select(Candidate)
        .order_by(Candidate.created_at.desc(), Candidate.id)
        .offset(offset)
        .limit(limit)
        .subquery()
    )
    candidate = aliased(Candidate, page)
    latest = (
        select(Application)
        .where(Application.candidate_id == candidate.id)
        .order_by(Application.created_at.desc(), Application.id.desc())
        .limit(1)
        .correlate(candidate)
        .lateral()
    )
    app = aliased(Application, latest)
    other = aliased(RoleAssessment)
    unassessed = (
        select(other.id)
        .where(
            other.evaluation_id == RoleAssessment.evaluation_id,
            or_(
                other.state != "completed",
                other.recommendation != "recommended_rejection",
                other.provisional.is_(True),
                other.score.is_(None),
                other.score >= NO_MATCH_SCORE_CUTOFF,
            ),
        )
        .correlate(RoleAssessment)
        .exists()
    )
    no_match = and_(
        RoleAssessment.recommendation == "recommended_rejection",
        RoleAssessment.score < NO_MATCH_SCORE_CUTOFF,
        ~unassessed,
    )
    chosen_opening = case(
        (
            and_(RoleAssessment.id.is_not(None), app.status != "shortlisted"),
            RoleAssessment.opening_id,
        ),
        else_=app.opening_id,
    )
    snapshot = func.json_array_elements(MatchingEvaluation.role_snapshot).table_valued(
        column("value", JSON)
    )
    snapshot_title = (
        select(
            func.coalesce(
                func.nullif(snapshot.c.value["jd"]["title"].as_string(), ""),
                snapshot.c.value["title"].as_string(),
            )
        )
        .where(
            snapshot.c.value["opening_id"].as_string() == cast(RoleAssessment.opening_id, String)
        )
        .correlate(MatchingEvaluation, RoleAssessment)
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(candidate, app, snapshot_title, RoleAssessment, MatchRun, Resume, Opening, no_match)
        .select_from(candidate)
        .outerjoin(app, true())
        .outerjoin(
            MatchingEvaluation,
            and_(
                MatchingEvaluation.id == app.latest_evaluation_id,
                MatchingEvaluation.resume_id == app.current_resume_id,
                MatchingEvaluation.state == "completed",
            ),
        )
        .outerjoin(
            RoleAssessment,
            and_(
                RoleAssessment.evaluation_id == MatchingEvaluation.id,
                or_(
                    and_(app.status == "shortlisted", RoleAssessment.opening_id == app.opening_id),
                    and_(
                        app.status != "shortlisted",
                        RoleAssessment.id == MatchingEvaluation.recommended_assessment_id,
                    ),
                ),
            ),
        )
        .outerjoin(
            MatchRun,
            and_(MatchRun.id == app.latest_match_id, MatchRun.resume_id == app.current_resume_id),
        )
        .outerjoin(Resume, Resume.id == app.current_resume_id)
        .outerjoin(Opening, Opening.id == chosen_opening)
        .options(
            load_only(
                candidate.id,
                candidate.profile,
                candidate.contact_revision,
                candidate.current_source_at,
                raiseload=True,
            ),
            load_only(app.id, app.status, raiseload=True),
            load_only(RoleAssessment.score, RoleAssessment.opening_id, raiseload=True),
            load_only(MatchRun.score, raiseload=True),
            load_only(Resume.upload_id, raiseload=True),
            load_only(Opening.title, raiseload=True),
        )
        .order_by(candidate.created_at.desc(), candidate.id)
    )
    result = []
    for row, application, frozen_title, role, run, resume, opening, unmatched in session.execute(
        query
    ):
        hidden = bool(unmatched) and application.status != "shortlisted" if application else False
        title = opening.title if opening else None
        if role and application.status != "shortlisted":
            title = frozen_title or title
        result.append(
            {
                **candidate_read(row),
                "application_id": application.id if application else None,
                "status": application.status if application else "processing",
                "opening_id": None if hidden else (opening.id if opening else None),
                "opening_title": None if hidden else title,
                "score": role.score if role else (run.score if run else None),
                "no_matching_opening": hidden,
                "latest_upload_id": resume.upload_id if resume else None,
            }
        )
    return result


def review_read(row: IdentityReview) -> dict[str, Any]:
    return {
        "id": row.id,
        "upload_id": row.upload_id,
        "message_id": row.message_id,
        "candidate_id": row.candidate_id,
        "kind": row.kind,
        "status": row.status,
        "proposed_contacts": row.proposed_contacts,
        "candidate_options": row.candidate_options,
        "decision": row.decision,
        "reason": row.reason,
        "actor_id": row.actor_id,
        "resolved_at": row.resolved_at,
    }


class CandidateService:
    def __init__(self, tenants: TenancyService) -> None:
        self.tenants = tenants

    def list(self, actor: Principal, tenant: UUID, limit: int, offset: int) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            return {
                "items": candidate_page(session, limit, offset),
                "limit": limit,
                "offset": offset,
            }

    def get(self, actor: Principal, tenant: UUID, identifier: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            row = session.get(Candidate, identifier)
            if not row:
                raise missing()
            revisions = session.scalars(
                select(ContactRevision)
                .where(ContactRevision.candidate_id == identifier)
                .order_by(ContactRevision.revision.desc())
                .limit(100)
            )
            return {
                **candidate_read(row, session),
                "contact_history": [
                    {
                        "revision": r.revision,
                        "before": r.before,
                        "after": r.after,
                        "reason": r.reason,
                        "actor_id": r.actor_id,
                        "created_at": r.created_at,
                    }
                    for r in revisions
                ],
            }

    def patch(
        self, actor: Principal, tenant: UUID, identifier: UUID, values: dict[str, Any]
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            advisory_lock(session, f"tenant:{tenant}")
            candidate = session.scalar(
                select(Candidate).where(Candidate.id == identifier).with_for_update()
            )
            if not candidate:
                raise missing()
            correct_contacts(
                session, actor, candidate, values, values["expected_revision"], values["reason"]
            )
            emit(session, tenant, "candidate.contacts_corrected", candidate.id)
            return candidate_read(candidate)

    def resumes(
        self, actor: Principal, tenant: UUID, identifier: UUID, limit: int, offset: int
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            if not session.get(Candidate, identifier):
                raise missing()
            rows = session.execute(
                select(Resume, CVUpload, Application)
                .join(CVUpload, Resume.upload_id == CVUpload.id)
                .join(Application, Resume.application_id == Application.id)
                .where(Application.candidate_id == identifier)
                .order_by(CVUpload.source_received_at.desc(), CVUpload.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return {
                "items": [
                    {
                        "id": r.id,
                        "version": r.version,
                        "upload_id": u.id,
                        "application_id": a.id,
                        "status": u.status,
                        "review_reason": u.review_reason,
                        "source_received_at": u.source_received_at,
                        "current": a.current_resume_id == r.id,
                    }
                    for r, u, a in rows
                ],
                "limit": limit,
                "offset": offset,
            }

    def file(self, actor: Principal, tenant: UUID, identifier: UUID) -> tuple[bytes, str]:
        with self.tenants.scope(actor, tenant) as session:
            upload = session.get(CVUpload, identifier)
            file = session.get(ResumeFile, upload.file_id) if upload else None
            if not file:
                raise missing()
            return file.content, file.mime_type

    def intake(self, actor: Principal, tenant: UUID, limit: int, offset: int) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            rows = session.execute(
                select(MailAttachment, MailMessage, Mailbox)
                .join(MailMessage, MailAttachment.message_id == MailMessage.id)
                .join(Mailbox, MailMessage.connection_id == Mailbox.id)
                .order_by(MailMessage.received_at.desc(), MailAttachment.id)
                .offset(offset)
                .limit(limit)
            )
            items = []
            for item, message, mailbox in rows:
                upload = session.get(CVUpload, item.upload_id) if item.upload_id else None
                items.append(
                    {
                        "id": item.id,
                        "connection_id": mailbox.id,
                        "provider": mailbox.provider,
                        "filename": item.filename,
                        "received_at": message.received_at,
                        "status": item.status,
                        "failure_code": item.failure_code,
                        "upload_id": item.upload_id,
                        "processing_status": upload.status if upload else None,
                        "review_reason": upload.review_reason if upload else None,
                        "application_id": upload.application_id if upload else None,
                    }
                )
            return {"items": items, "limit": limit, "offset": offset}

    def reviews(self, actor: Principal, tenant: UUID, limit: int, offset: int) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            rows = session.scalars(
                select(IdentityReview)
                .order_by(IdentityReview.created_at.desc(), IdentityReview.id)
                .offset(offset)
                .limit(limit)
            )
            return {"items": [review_read(row) for row in rows], "limit": limit, "offset": offset}

    def review(self, actor: Principal, tenant: UUID, identifier: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            row = session.get(IdentityReview, identifier)
            if not row:
                raise missing()
            options = session.scalars(
                select(Candidate).where(
                    Candidate.id.in_([UUID(item) for item in row.candidate_options])
                )
            )
            return {**review_read(row), "candidates": [candidate_read(item) for item in options]}

    def resolve(
        self, actor: Principal, tenant: UUID, identifier: UUID, values: dict[str, Any], key: str
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            prior = existing_request(
                session, tenant, "identity.resolve", key, {"id": identifier, **values}
            )
            advisory_lock(session, f"tenant:{tenant}")
            row = session.scalar(
                select(IdentityReview).where(IdentityReview.id == identifier).with_for_update()
            )
            if not row:
                raise missing()
            if prior:
                return review_read(row)
            if row.status != "open":
                raise AppError("REVIEW_RESOLVED", "This case already has a recorded decision.", 409)
            action = values["decision"]
            if row.kind == "cross_role_submission":
                if action != "dismiss":
                    raise AppError(
                        "INVALID_DECISION",
                        "Keep the existing opening assignment for this submission.",
                        422,
                    )
            elif row.kind == "identity_conflict":
                upload = session.get(CVUpload, row.upload_id)
                extraction = session.scalar(
                    select(ResumeExtraction).where(ResumeExtraction.upload_id == row.upload_id)
                )
                if not upload or not extraction:
                    raise missing()
                if action == "link":
                    candidate = (
                        session.get(Candidate, values.get("candidate_id"))
                        if values.get("candidate_id")
                        else None
                    )
                    if not candidate:
                        raise missing()
                elif action == "create":
                    profile = extraction.profile
                    available = {}
                    for kind in ("email", "mobile"):
                        field = "phone" if kind == "mobile" and "phone" in profile else kind
                        value = profile.get(field)
                        if value and not session.scalar(
                            select(Contact.id).where(Contact.kind == kind, Contact.value == value)
                        ):
                            available[kind] = value
                    candidate = Candidate(
                        id=uuid4(),
                        tenant_id=tenant,
                        profile=with_contacts(
                            profile, {kind: available.get(kind) for kind in ("email", "mobile")}
                        ),
                    )
                    session.add(candidate)
                    session.flush()
                    session.add_all(
                        Contact(tenant_id=tenant, candidate_id=candidate.id, kind=kind, value=value)
                        for kind, value in available.items()
                    )
                else:
                    raise AppError(
                        "INVALID_DECISION", "Link a candidate or create a separate candidate.", 422
                    )
                row.candidate_id = candidate.id
                upload.resolved_candidate_id, upload.identity_choice = candidate.id, action
                emit(
                    session,
                    tenant,
                    "cv.uploaded",
                    upload.id,
                )
            else:
                candidate = session.get(Candidate, row.candidate_id) if row.candidate_id else None
                if not candidate:
                    raise missing()
                if action == "accept" and row.kind == "contact_proposal":
                    if not values.get("expected_revision"):
                        raise AppError(
                            "REVISION_REQUIRED", "Supply the current contact revision.", 422
                        )
                    correct_contacts(
                        session,
                        actor,
                        candidate,
                        row.proposed_contacts,
                        values["expected_revision"],
                        values["reason"],
                    )
                elif action != "dismiss":
                    raise AppError(
                        "INVALID_DECISION",
                        "Accept the proposal or dismiss this contact issue.",
                        422,
                    )
            row.status, row.decision, row.reason = "resolved", action, values["reason"]
            row.actor_id, row.resolved_at = actor.user_id, utcnow()
            emit(session, tenant, "identity.resolved", row.id)
            remember_request(
                session, tenant, "identity.resolve", key, {"id": identifier, **values}, row.id
            )
            return review_read(row)
