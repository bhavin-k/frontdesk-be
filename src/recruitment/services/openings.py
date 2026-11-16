"""Tenant-scoped opening management and lifecycle transactions."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import CriteriaVersion, CVUpload, JobCriterion, Opening, Outbox
from recruitment.adapters.db.openings import OpeningDocument, OpeningIntakePeriod
from recruitment.adapters.documents import validate_file
from recruitment.adapters.providers import Providers
from recruitment.core.config import ScoringStrategy, Settings
from recruitment.core.errors import AppError, ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.openings import (
    SHORTLIST_THRESHOLD,
    OpeningAction,
    OpeningCreate,
    OpeningUpdate,
)
from recruitment.services.auth import Principal
from recruitment.services.common import emit, existing_request, missing, remember_request
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


def intake_allowed(session: Session, opening: Opening, received_at: datetime | None) -> bool:
    if opening.status == "closed":
        return False
    if received_at is None:
        return opening.status == "active"
    return (
        session.scalar(
            select(OpeningIntakePeriod.id)
            .where(
                OpeningIntakePeriod.opening_id == opening.id,
                OpeningIntakePeriod.starts_at <= received_at,
                or_(
                    OpeningIntakePeriod.ends_at.is_(None), OpeningIntakePeriod.ends_at > received_at
                ),
            )
            .limit(1)
        )
        is not None
    )


def release_waiting(session: Session, opening: Opening, strategy: ScoringStrategy) -> None:
    uploads = session.scalars(
        select(CVUpload).where(
            CVUpload.opening_id == opening.id,
            CVUpload.status == "queued",
            CVUpload.retention_expires_at > utcnow(),
        )
    )
    for upload in uploads:
        key = f"cv.uploaded:{upload.id}"
        if not session.scalar(select(Outbox.id).where(Outbox.dedupe_key == key)):
            emit(
                session, opening.tenant_id, "cv.uploaded", upload.id, key, scoring_strategy=strategy
            )


class OpeningService:
    def __init__(self, tenants: TenancyService, providers: Providers, settings: Settings) -> None:
        self.tenants, self.providers = tenants, providers
        self.settings = settings

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
        version = session.scalar(
            select(CriteriaVersion)
            .where(CriteriaVersion.opening_id == row.id)
            .order_by(CriteriaVersion.version.desc())
        )
        criteria = (
            list(
                session.scalars(
                    select(JobCriterion)
                    .where(JobCriterion.version_id == version.id)
                    .order_by(JobCriterion.position)
                )
            )
            if version
            else []
        )
        documents = session.scalars(
            select(OpeningDocument)
            .where(OpeningDocument.opening_id == row.id)
            .order_by(OpeningDocument.created_at, OpeningDocument.id)
        )
        return {
            "id": row.id,
            "title": row.title,
            "job_description": row.job_description,
            "status": row.status,
            "details": row.details,
            "questions": row.questions,
            "revision": row.revision,
            "source_revision": row.source_revision,
            "criteria_status": row.criteria_status,
            "criteria_error": row.criteria_error,
            "criteria_version_id": version.id if version else None,
            "shortlist_threshold": version.shortlist_threshold if version else SHORTLIST_THRESHOLD,
            "criteria_metadata": version.processing_metadata if version else {},
            "documents": [OpeningService.document_read(item) for item in documents],
            "criteria": [
                {
                    "id": c.id,
                    "text": c.text,
                    "required": c.required,
                    "strict": c.strict,
                    "weight": c.weight,
                    "source_evidence": c.source_evidence,
                }
                for c in criteria
            ],
        }

    def read(self, actor: Principal, tenant: UUID, opening: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            return self.read_session(session, opening)

    def list_openings(
        self, actor: Principal, tenant: UUID, limit: int, offset: int, status: str | None
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            query = select(Opening.id)
            if status:
                query = query.where(Opening.status == status)
            ids = session.scalars(
                query.order_by(Opening.created_at.desc(), Opening.id).offset(offset).limit(limit)
            )
            return {
                "items": [self.read_session(session, id) for id in ids],
                "limit": limit,
                "offset": offset,
            }

    def check_files(self, actor: Principal, tenant: UUID, files: list[JobFile]) -> None:
        with self.tenants.scope(actor, tenant):
            pass
        if len(files) > MAX_DOCUMENTS:
            raise AppError("DOCUMENT_LIMIT", "An opening supports up to five documents.")
        for file in files:
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

    @staticmethod
    def prepare(session: Session, row: Opening) -> None:
        row.criteria_status, row.criteria_error = "queued", None
        row.preparation_config = {}
        emit(
            session,
            row.tenant_id,
            "opening.prepare",
            row.id,
            f"opening.prepare:{row.id}:{row.source_revision}:{row.revision}",
        )

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
            session.add(
                OpeningIntakePeriod(tenant_id=tenant, opening_id=row.id, starts_at=utcnow())
            )
            self.add_files(session, row, files)
            self.prepare(session, row)
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
            details = payload.details.model_dump(mode="json")
            operational = {
                "employment_type",
                "salary_min",
                "salary_max",
                "headcount",
                "application_deadline",
            }
            changed = (
                row.title != payload.title
                or row.job_description != payload.job_description
                or any(row.details.get(k) != v for k, v in details.items() if k not in operational)
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
                self.prepare(session, row)
            session.flush()
            return self.read_session(session, row.id)

    def action(
        self, actor: Principal, tenant: UUID, opening: UUID, payload: OpeningAction
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
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
            current = session.scalar(
                select(OpeningIntakePeriod)
                .where(
                    OpeningIntakePeriod.opening_id == row.id, OpeningIntakePeriod.ends_at.is_(None)
                )
                .with_for_update()
            )
            if current:
                current.ends_at = now
                session.flush()
            row.status, row.revision = target, row.revision + 1
            if target == "active":
                session.add(OpeningIntakePeriod(tenant_id=tenant, opening_id=row.id, starts_at=now))
            if target == "closed":
                if row.criteria_status != "ready":
                    row.criteria_status, row.criteria_error = "failed", "OPENING_CLOSED"
                emit(
                    session,
                    tenant,
                    "opening.prepare",
                    row.id,
                    f"opening.closed:{row.id}:{row.revision}",
                )
            session.flush()
            from recruitment.services.matching import requeue_pending

            requeue_pending(
                session,
                tenant,
                f"lifecycle:{row.id}:{row.revision}",
                self.settings.scoring_strategy,
            )
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
                row.revision, row.source_revision = row.revision + 1, row.source_revision + 1
                self.prepare(session, row)
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
            row.revision, row.source_revision = row.revision + 1, row.source_revision + 1
            self.prepare(session, row)
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

    def retry(self, actor: Principal, tenant: UUID, opening: UUID, expected: int) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            row = self.locked(session, opening, expected)
            if row.status == "closed" or row.criteria_status != "failed":
                raise AppError(
                    "INVALID_OPENING_TRANSITION", "Only failed requirements can be retried.", 409
                )
            row.revision += 1
            self.prepare(session, row)
            session.flush()
            return self.read_session(session, row.id)
