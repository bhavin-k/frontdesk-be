"""Tenant-scoped opening management and lifecycle transactions."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session, load_only

from recruitment.adapters.db.models import Opening
from recruitment.adapters.db.openings import OpeningDocument, OpeningIntakePeriod
from recruitment.adapters.documents import DOCUMENT_TYPES, validate_file
from recruitment.adapters.providers import Providers
from recruitment.core.errors import AppError, ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.contracts import JobDescription
from recruitment.domain.openings import (
    OpeningAction,
    OpeningCreate,
    OpeningUpdate,
)
from recruitment.domain.scoring_contracts import POLICY_VERSION
from recruitment.services.auth import Principal
from recruitment.services.common import advisory_lock, existing_request, missing, remember_request
from recruitment.services.scoring_rubrics import prepare_rubric, saved_rubric
from recruitment.services.tenancy import TenancyService

MAX_DOCUMENTS = 5


@dataclass(frozen=True)
class JobFile:
    name: str
    mime_type: str
    content: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def open_at(received_at: datetime) -> ColumnElement[bool]:
    return (OpeningIntakePeriod.starts_at <= received_at) & or_(
        OpeningIntakePeriod.ends_at.is_(None), OpeningIntakePeriod.ends_at > received_at
    )


def openings_at(session: Session, received_at: datetime) -> list[dict[str, Any]]:
    return [
        {
            "opening_id": str(period.opening_id),
            "jd": period.match_description,
            "scoring_policy": POLICY_VERSION,
        }
        for period in session.scalars(
            select(OpeningIntakePeriod)
            .where(open_at(received_at))
            .order_by(OpeningIntakePeriod.opening_id)
        )
    ]


def intake_allowed(session: Session, opening: Opening, received_at: datetime | None) -> bool:
    if received_at is None:
        return opening.status == "active"
    return (
        session.scalar(
            select(OpeningIntakePeriod.id)
            .where(OpeningIntakePeriod.opening_id == opening.id, open_at(received_at))
            .limit(1)
        )
        is not None
    )


def job_description(title: str, description: str, details: dict[str, Any]) -> JobDescription:
    return JobDescription(
        title=title,
        description=description,
        requirements=details.get("candidate_requirements") or "",
        min_experience_years=details.get("total_experience_years") or 0,
        relevant_experience_years=details.get("relevant_experience_years"),
        domain_experience=details.get("domain_experience") or "",
    )


def start_period(session: Session, opening: Opening, starts_at: datetime) -> None:
    jd = job_description(opening.title, opening.job_description, opening.details)
    jd.rubric = saved_rubric(session, jd)
    session.add(
        OpeningIntakePeriod(
            tenant_id=opening.tenant_id,
            opening_id=opening.id,
            starts_at=starts_at,
            match_description=jd.model_dump(),
        )
    )


def end_period(session: Session, opening: Opening, ends_at: datetime) -> None:
    period = session.scalar(
        select(OpeningIntakePeriod)
        .where(OpeningIntakePeriod.opening_id == opening.id, OpeningIntakePeriod.ends_at.is_(None))
        .with_for_update()
    )
    if period is not None:
        period.ends_at = ends_at
        session.flush()


class OpeningService:
    def __init__(self, tenants: TenancyService, providers: Providers) -> None:
        self.tenants, self.providers = tenants, providers

    @staticmethod
    def locked(session: Session, opening_id: UUID, expected: int | None = None) -> Opening:
        row = session.scalar(select(Opening).where(Opening.id == opening_id).with_for_update())
        if not row:
            raise missing()
        if expected is not None and row.revision != expected:
            raise AppError(
                "OPENING_REVISION_CONFLICT", "This opening changed. Reload and retry.", 409
            )
        return row

    @staticmethod
    def document_read(row: OpeningDocument) -> dict[str, Any]:
        return {
            "id": row.id,
            "file_name": row.file_name,
            "mime_type": row.mime_type,
            "byte_length": row.byte_length,
            "created_at": row.created_at,
        }

    @staticmethod
    def read_session(session: Session, opening_id: UUID) -> dict[str, Any]:
        row = session.get(Opening, opening_id)
        if not row:
            raise missing()
        documents = session.scalars(
            select(OpeningDocument)
            .where(OpeningDocument.opening_id == row.id)
            .order_by(OpeningDocument.created_at, OpeningDocument.id)
        )
        return OpeningService.opening_read(row, list(documents))

    @staticmethod
    def opening_read(row: Opening, documents: list[OpeningDocument]) -> dict[str, Any]:
        return {
            "id": row.id,
            "title": row.title,
            "job_description": row.job_description,
            "status": row.status,
            "details": row.details,
            "questions": row.questions,
            "revision": row.revision,
            "source_revision": row.source_revision,
            "documents": [OpeningService.document_read(item) for item in documents],
        }

    def read(self, actor: Principal, tenant: UUID, opening: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            return self.read_session(session, opening)

    def list_openings(
        self, actor: Principal, tenant: UUID, limit: int, offset: int, status: str | None
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            query = select(Opening)
            if status:
                query = query.where(Opening.status == status)
            rows = list(
                session.scalars(
                    query.order_by(Opening.created_at.desc(), Opening.id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            documents: dict[UUID, list[OpeningDocument]] = {row.id: [] for row in rows}
            if rows:
                for document in session.scalars(
                    select(OpeningDocument)
                    .where(OpeningDocument.opening_id.in_(documents))
                    .options(
                        load_only(
                            OpeningDocument.id,
                            OpeningDocument.opening_id,
                            OpeningDocument.file_name,
                            OpeningDocument.mime_type,
                            OpeningDocument.byte_length,
                            OpeningDocument.created_at,
                        )
                    )
                    .order_by(OpeningDocument.created_at, OpeningDocument.id)
                ):
                    documents[document.opening_id].append(document)
            return {
                "items": [self.opening_read(row, documents[row.id]) for row in rows],
                "limit": limit,
                "offset": offset,
            }

    def check_files(self, actor: Principal, tenant: UUID, files: list[JobFile]) -> None:
        with self.tenants.scope(actor, tenant):
            pass
        if len(files) > MAX_DOCUMENTS:
            raise AppError("DOCUMENT_LIMIT", "An opening supports up to five documents.")
        for file in files:
            if file.mime_type not in DOCUMENT_TYPES:
                raise AppError("UNSUPPORTED_FILE", "Upload a PDF, DOCX or DOC file.", 415)
            validate_file(file.content, file.mime_type)
            try:
                self.providers.scanner.scan(file.content)
            except ProviderError as exc:
                raise AppError(
                    exc.code, "The document security scan could not complete.", 503
                ) from None

    @staticmethod
    def add_files(session: Session, row: Opening, files: list[JobFile]) -> bool:
        existing = list(
            session.scalars(select(OpeningDocument).where(OpeningDocument.opening_id == row.id))
        )
        digests = {file.sha256 for file in existing}
        fresh = {file.digest: file for file in files if file.digest not in digests}
        if len(existing) + len(fresh) > MAX_DOCUMENTS:
            raise AppError("DOCUMENT_LIMIT", "An opening supports up to five documents.")
        for file in fresh.values():
            name = file.name.replace("\\", "/").split("/")[-1]
            name = "".join(c for c in name if c.isprintable())[:200] or "document"
            session.add(
                OpeningDocument(
                    tenant_id=row.tenant_id,
                    opening_id=row.id,
                    file_name=name,
                    mime_type=file.mime_type,
                    byte_length=len(file.content),
                    content=file.content,
                    sha256=file.digest,
                )
            )
        return bool(fresh)

    def create(
        self,
        actor: Principal,
        tenant: UUID,
        payload: OpeningCreate,
        files: list[JobFile],
        key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        self.check_files(actor, tenant, files)
        fingerprint = {
            **payload.model_dump(mode="json"),
            "files": [{"hash": f.digest, "mime": f.mime_type, "name": f.name} for f in files],
        }
        with self.tenants.scope(actor, tenant) as session:
            previous = existing_request(session, tenant, "opening", key, fingerprint)
            if previous:
                return self.read_session(session, previous), False
            prepare_rubric(
                session,
                self.providers.rubrics,
                job_description(
                    payload.title, payload.job_description, payload.details.model_dump()
                ),
                tenant,
            )
        with self.tenants.scope(actor, tenant) as session:
            advisory_lock(session, f"tenant:{tenant}")
            previous = existing_request(session, tenant, "opening", key, fingerprint)
            if previous:
                return self.read_session(session, previous), False
            row = Opening(
                id=uuid4(),
                tenant_id=tenant,
                title=payload.title,
                job_description=payload.job_description,
                details=payload.details.model_dump(mode="json"),
                questions=payload.questions,
                revision=1,
                source_revision=1,
                status="active",
            )
            session.add(row)
            session.flush()
            start_period(session, row, utcnow())
            self.add_files(session, row, files)
            remember_request(session, tenant, "opening", key, fingerprint, row.id)
            session.flush()
            return self.read_session(session, row.id), True

    def update(
        self, actor: Principal, tenant: UUID, opening: UUID, payload: OpeningUpdate
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            row = self.locked(session, opening, payload.expected_revision)
            if row.status == "closed":
                raise AppError("OPENING_CLOSED", "Closed openings cannot be edited.", 409)
            prepare_rubric(
                session,
                self.providers.rubrics,
                job_description(
                    payload.title, payload.job_description, payload.details.model_dump()
                ),
                tenant,
            )
        with self.tenants.scope(actor, tenant) as session:
            advisory_lock(session, f"tenant:{tenant}")
            row = self.locked(session, opening, payload.expected_revision)
            if row.status == "closed":
                raise AppError("OPENING_CLOSED", "Closed openings cannot be edited.", 409)
            details = payload.details.model_dump(mode="json")
            matching_fields = (
                "candidate_requirements",
                "total_experience_years",
                "relevant_experience_years",
                "domain_experience",
            )
            changed = (
                row.title != payload.title
                or row.job_description != payload.job_description
                or any(row.details.get(key) != details.get(key) for key in matching_fields)
            )
            row.title, row.job_description, row.details, row.questions = (
                payload.title,
                payload.job_description,
                details,
                payload.questions,
            )
            row.revision += 1
            if changed:
                row.source_revision += 1
            session.flush()
            if changed and row.status == "active":
                now = utcnow()
                end_period(session, row, now)
                start_period(session, row, now)
                session.flush()
            return self.read_session(session, row.id)

    def action(
        self, actor: Principal, tenant: UUID, opening: UUID, payload: OpeningAction
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            advisory_lock(session, f"tenant:{tenant}")
            row = self.locked(session, opening, payload.expected_revision)
            targets = {"pause": ("active", "paused"), "continue": ("paused", "active")}
            if payload.action == "close":
                if row.status == "closed":
                    return self.read_session(session, row.id)
                target = "closed"
            else:
                source, target = targets[payload.action]
                if row.status != source:
                    raise AppError(
                        "INVALID_OPENING_TRANSITION",
                        "This action is not available for the opening.",
                        409,
                    )
            now = utcnow()
            if row.status == "active":
                end_period(session, row, now)
            row.status, row.revision = target, row.revision + 1
            if target == "active":
                start_period(session, row, now)
            session.flush()
            return self.read_session(session, row.id)

    def documents(
        self, actor: Principal, tenant: UUID, opening: UUID, files: list[JobFile], expected: int
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            self.locked(session, opening, expected)
        self.check_files(actor, tenant, files)
        with self.tenants.scope(actor, tenant) as session:
            row = self.locked(session, opening, expected)
            if row.status == "closed":
                raise AppError("OPENING_CLOSED", "Closed openings cannot be edited.", 409)
            if self.add_files(session, row, files):
                row.revision += 1
            session.flush()
            return self.read_session(session, row.id)

    def remove_document(
        self, actor: Principal, tenant: UUID, opening: UUID, document: UUID, expected: int
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            row = self.locked(session, opening, expected)
            item = session.get(OpeningDocument, document)
            if not item or item.opening_id != opening:
                raise missing()
            if row.status == "closed":
                raise AppError("OPENING_CLOSED", "Closed openings cannot be edited.", 409)
            session.delete(item)
            row.revision += 1
            session.flush()
            return self.read_session(session, row.id)

    def file(
        self, actor: Principal, tenant: UUID, opening: UUID, document: UUID
    ) -> OpeningDocument:
        with self.tenants.scope(actor, tenant) as session:
            self.read_session(session, opening)
            item = session.get(OpeningDocument, document)
            if not item or item.opening_id != opening:
                raise missing()
            session.expunge(item)
            return item
