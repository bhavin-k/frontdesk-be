import hashlib
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from recruitment.adapters.db.models import (
    Application,
    Candidate,
    Contact,
    CVUpload,
    Opening,
    Resume,
    ResumeExtraction,
    ResumeFile,
    RetentionSetting,
)
from recruitment.adapters.db.phase2 import DocumentDerivative, IdentityReview
from recruitment.adapters.documents import PDF, WORD_TYPES
from recruitment.adapters.paddle_ocr import MAX_TEXT_CHARS
from recruitment.adapters.pdf_safety import PROCESSING_VERSION
from recruitment.adapters.providers import PARSER_PROMPT, Providers
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.contracts import CandidateProfile
from recruitment.domain.profiles import normalize_profile, with_contacts
from recruitment.domain.retention import deadline
from recruitment.domain.tokenization import token_count
from recruitment.ports.queue import Lease, WorkerQueue
from recruitment.services.common import emit
from recruitment.services.document_processing import prepare_pdf


class CVProcessor:
    def __init__(self, queue: WorkerQueue, providers: Providers) -> None:
        self.queue, self.providers = queue, providers

    def _parse(self, lease: Lease) -> CandidateProfile:
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            if not upload:
                raise ProviderError("UPLOAD_NOT_FOUND")
            if upload.retention_expires_at <= utcnow():
                raise ProviderError("RETENTION_EXPIRED")
            cached = session.scalar(
                select(ResumeExtraction).where(ResumeExtraction.upload_id == upload.id)
            )
            if cached and cached.prompt_version in {"cv-parser-v1", "cv-parser-v2", PARSER_PROMPT}:
                return CandidateProfile.model_validate(cached.profile)
            file = session.get(ResumeFile, upload.file_id)
            assert file is not None
            content, mime_type = file.content, file.mime_type
            upload.status = "extracting"

        if mime_type in WORD_TYPES:
            content = self._convert(lease, content, mime_type)
            mime_type = PDF
        sanitized = False
        if mime_type == PDF:
            prepared = prepare_pdf(content, self.providers.scanner)
            sanitized = prepared is not content
            content = prepared
        extraction = self.providers.ocr.extract(content, mime_type)
        raw_text = "\n".join(page.text for page in extraction.pages)
        if not any(character.isalnum() for character in raw_text) or extraction.confidence < 0.8:
            raise ProviderError("LOW_CONFIDENCE_EXTRACTION")
        if len(raw_text) > MAX_TEXT_CHARS:
            raise ProviderError("EXTRACTED_TEXT_TOO_LARGE")
        if token_count(raw_text) > self.queue.settings.cv_token_ceiling:
            raise ProviderError("CV_TOKEN_CEILING_EXCEEDED")
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            assert upload is not None
            upload.status = "profiling"
        profile = normalize_profile(self.providers.profiles.profile(raw_text))
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            assert upload is not None
            if upload.retention_expires_at <= utcnow():
                raise ProviderError("RETENTION_EXPIRED")
            cached = session.scalar(
                select(ResumeExtraction).where(ResumeExtraction.upload_id == upload.id)
            )
            if cached is None:
                cached = ResumeExtraction(tenant_id=lease.tenant_id, upload_id=upload.id)
                session.add(cached)
            cached.pages = [page.model_dump() for page in extraction.pages]
            cached.method = (
                f"{extraction.method}+{PROCESSING_VERSION}" if sanitized else extraction.method
            )
            cached.confidence = Decimal(str(extraction.confidence))
            cached.profile = profile.model_dump()
            cached.profile_model = self.providers.profiles.model
            cached.prompt_version = PARSER_PROMPT
        return profile

    def _convert(self, lease: Lease, content: bytes, mime_type: str) -> bytes:
        with self.queue.owned(lease) as session:
            cached = session.scalar(
                select(DocumentDerivative).where(DocumentDerivative.upload_id == lease.aggregate_id)
            )
            if cached:
                return cached.content
        converter = self.providers.converter
        converted = converter.convert(content, mime_type)
        self.providers.scanner.scan(converted)
        with self.queue.owned(lease) as session:
            session.add(
                DocumentDerivative(
                    tenant_id=lease.tenant_id,
                    upload_id=lease.aggregate_id,
                    content=converted,
                    sha256=hashlib.sha256(converted).hexdigest(),
                    converter_version=converter.version,
                )
            )
        return converted

    def _resolve(self, lease: Lease, profile: CandidateProfile) -> UUID | None:
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            assert upload is not None
            existing_resume = session.scalar(select(Resume).where(Resume.upload_id == upload.id))
            if existing_resume:
                app = session.get(Application, existing_resume.application_id)
                if app and app.current_resume_id == existing_resume.id and not app.rejected_at:
                    candidate = session.get(Candidate, app.candidate_id)
                    if candidate and "total_experience_years" not in candidate.profile:
                        candidate.profile = with_contacts(
                            profile.model_dump(),
                            {
                                "name": candidate.profile.get("name"),
                                "email": candidate.profile.get("email"),
                                "mobile": candidate.profile.get("mobile"),
                                **candidate.contact_overrides,
                            },
                        )
                return existing_resume.id
            identities = [
                (kind, value)
                for kind, value in (("email", profile.email), ("mobile", profile.phone))
                if value
            ]
            matches: set[UUID] = set()
            for kind, value in identities:
                match = session.scalar(
                    select(Contact.candidate_id).where(Contact.kind == kind, Contact.value == value)
                )
                if match:
                    matches.add(match)
            candidate = (
                session.get(Candidate, upload.resolved_candidate_id)
                if upload.resolved_candidate_id
                else (session.get(Candidate, next(iter(matches))) if len(matches) == 1 else None)
            )
            conflict = len(matches) > 1
            if candidate:
                contacts = session.scalars(
                    select(Contact).where(Contact.candidate_id == candidate.id)
                ).all()
                known = {contact.kind: contact.value for contact in contacts}
                conflict |= any(
                    kind in known and known[kind] != value for kind, value in identities
                )
            if conflict and not upload.identity_choice:
                if not session.scalar(
                    select(IdentityReview.id).where(IdentityReview.upload_id == upload.id)
                ):
                    session.add(
                        IdentityReview(
                            tenant_id=lease.tenant_id,
                            upload_id=upload.id,
                            proposed_contacts={
                                "name": profile.name,
                                "email": profile.email,
                                "mobile": profile.phone,
                            },
                            candidate_options=sorted(str(item) for item in matches),
                        )
                    )
                upload.status, upload.review_reason = "needs_review", "POSSIBLE_DUPLICATE"
                emit(session, lease.tenant_id, "review.required", upload.id)
                return None
            if not candidate:
                candidate = Candidate(
                    id=uuid4(), tenant_id=lease.tenant_id, profile=profile.model_dump()
                )
                session.add(candidate)
                session.flush()
            # Multiple openings must not attach another role's match to this application.
            active_app = session.scalar(
                select(Application)
                .where(Application.candidate_id == candidate.id, Application.rejected_at.is_(None))
                .with_for_update()
            )
            if active_app and active_app.opening_id != upload.opening_id:
                submitted_opening = session.get(Opening, upload.opening_id)
                existing_opening = session.get(Opening, active_app.opening_id)
                assert submitted_opening is not None and existing_opening is not None
                session.add(
                    IdentityReview(
                        tenant_id=lease.tenant_id,
                        upload_id=upload.id,
                        candidate_id=candidate.id,
                        kind="cross_role_submission",
                        target_opening_id=upload.opening_id,
                        candidate_options=[str(candidate.id)],
                        proposed_contacts={
                            "opening_id": str(upload.opening_id),
                            "opening_title": submitted_opening.title,
                            "existing_opening_id": str(active_app.opening_id),
                            "existing_opening_title": existing_opening.title,
                        },
                    )
                )
                upload.status, upload.review_reason = "needs_review", "CANDIDATE_HAS_ACTIVE_OPENING"
                emit(session, lease.tenant_id, "review.required", upload.id)
                return None
            source_key = str(upload.id)
            current = candidate.current_source_at is None or (
                upload.source_received_at,
                source_key,
            ) >= (candidate.current_source_at, candidate.current_source_key or "")
            if current:
                resolved_contacts: dict[str, Any] = {}
                if upload.identity_choice:
                    resolved_contacts = {"email": None, "mobile": None}
                    resolved_contacts.update(
                        {
                            c.kind: c.value
                            for c in session.scalars(
                                select(Contact).where(Contact.candidate_id == candidate.id)
                            )
                        }
                    )
                candidate.profile = with_contacts(
                    profile.model_dump(), {**resolved_contacts, **candidate.contact_overrides}
                )
                candidate.current_source_at, candidate.current_source_key = (
                    upload.source_received_at,
                    source_key,
                )
            for kind, value in identities:
                if upload.identity_choice or kind in candidate.contact_overrides:
                    continue
                if not session.scalar(
                    select(Contact.id).where(
                        Contact.candidate_id == candidate.id, Contact.kind == kind
                    )
                ):
                    session.add(
                        Contact(
                            tenant_id=lease.tenant_id,
                            candidate_id=candidate.id,
                            kind=kind,
                            value=value,
                        )
                    )
            app = session.scalar(
                select(Application)
                .where(Application.candidate_id == candidate.id, Application.rejected_at.is_(None))
                .with_for_update()
            )
            settings = session.scalar(select(RetentionSetting))
            assert settings is not None
            now = utcnow()
            if not app:
                previous = session.scalar(
                    select(Application.id).where(Application.candidate_id == candidate.id)
                )
                app = Application(
                    id=uuid4(),
                    tenant_id=lease.tenant_id,
                    candidate_id=candidate.id,
                    opening_id=upload.opening_id,
                    last_activity_at=now,
                    retention_expires_at=deadline(
                        now, None, other_days=settings.other_candidate_retention_days
                    ),
                )
                if previous:
                    upload.review_reason = "REAPPLICATION"
                session.add(app)
                session.flush()
            else:
                app.last_activity_at = now
                app.retention_expires_at = deadline(
                    now, None, other_days=settings.other_candidate_retention_days
                )
                upload.review_reason = "RESUME_UPDATED"
            upload.application_id = app.id
            upload.retention_expires_at = app.retention_expires_at
            versions = session.scalars(
                select(Resume.version).where(Resume.application_id == app.id)
            ).all()
            resume = Resume(
                id=uuid4(),
                tenant_id=lease.tenant_id,
                application_id=app.id,
                upload_id=upload.id,
                version=max(versions, default=0) + 1,
            )
            session.add(resume)
            session.flush()
            # Versions describe arrival/processing history; source time decides the current CV.
            current_resume = (
                session.get(Resume, app.current_resume_id) if app.current_resume_id else None
            )
            current_upload = (
                session.get(CVUpload, current_resume.upload_id) if current_resume else None
            )
            if not current_upload or (upload.source_received_at, str(upload.id)) > (
                current_upload.source_received_at,
                str(current_upload.id),
            ):
                app.current_resume_id = resume.id
                app.status = "processing"
            upload.status = "matching"
            return resume.id

    def process(self, lease: Lease) -> None:
        from recruitment.services.matching import MatchingWorkflow

        with self.queue.serialize_cv_scoring(lease):
            profile = self._parse(lease)
            resume = self._resolve(lease, profile)
            if resume is not None:
                MatchingWorkflow(self.queue, self.providers).process(lease, resume, profile)
