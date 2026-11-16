import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select

from recruitment.adapters.ai import validate_vectors
from recruitment.adapters.db.models import (
    Application,
    Candidate,
    Contact,
    CVUpload,
    Job,
    JobCriterion,
    Opening,
    Resume,
    ResumeChunk,
    ResumeExtraction,
    ResumeFile,
    RetentionSetting,
)
from recruitment.adapters.db.phase2 import (
    DocumentDerivative,
    EmbeddingBatch,
    IdentityReview,
    ProcessingSnapshot,
)
from recruitment.adapters.documents import DOC, PDF, needs_ocr
from recruitment.adapters.pdf_safety import PROCESSING_VERSION
from recruitment.adapters.providers import Providers
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.chunking import chunk_pages
from recruitment.domain.contracts import (
    CandidateProfile,
    Extraction,
    Page,
)
from recruitment.domain.full_text import FULL_TEXT_PROMPT_VERSION
from recruitment.domain.matching import scoring_text
from recruitment.domain.profiles import normalize_profile
from recruitment.domain.rag import embedding_batches
from recruitment.domain.retention import deadline
from recruitment.domain.tokenization import token_count
from recruitment.ports.queue import Lease, WorkerQueue
from recruitment.services.common import emit
from recruitment.services.document_processing import prepare_pdf

MAX_TEXT_CHARS = 200_000
logger = logging.getLogger(__name__)


class CVProcessor:
    def __init__(self, queue: WorkerQueue, providers: Providers) -> None:
        self.queue, self.providers = queue, providers

    def _snapshot(self, lease: Lease) -> dict[str, Any]:
        with self.queue.owned(lease) as session:
            job = session.get(Job, lease.id)
            assert job is not None
            strategy = job.scoring_strategy
            config: dict[str, Any] = self.queue.settings.processing_snapshot()
            if strategy == "full_text":
                grader = self.providers.full_text_grader
                if grader is None:
                    raise ProviderError("FULL_TEXT_GRADER_NOT_CONFIGURED")
                config = {
                    key: value
                    for key, value in config.items()
                    if key.startswith("llm_") or key in {"profile_model", "grader_model"}
                }
                config.update(
                    scoring_strategy=strategy,
                    actual_grader_model=grader.model,
                    grading_prompt=FULL_TEXT_PROMPT_VERSION,
                    cv_token_ceiling=self.queue.settings.cv_token_ceiling,
                    request_tokens=self.queue.settings.full_text_request_tokens,
                    output_tokens=self.queue.settings.full_text_output_tokens,
                )
                fingerprint = hashlib.sha256(
                    json.dumps(config, sort_keys=True).encode()
                ).hexdigest()
            else:
                config.update(
                    actual_embedding_model=self.providers.embeddings.model,
                    actual_grader_model=self.providers.grader.model,
                    grading_prompt="matching-v1",
                )
                fingerprint = self.providers.fingerprint
            config.update(
                actual_provider=self.providers.name,
                actual_profile_model=self.providers.profiles.model,
                profile_prompt="profile-v1",
            )
            snapshot = session.scalar(
                select(ProcessingSnapshot).where(
                    ProcessingSnapshot.upload_id == lease.aggregate_id,
                    ProcessingSnapshot.scoring_strategy == strategy,
                )
            )
            if snapshot:
                if snapshot.fingerprint != fingerprint or snapshot.config != config:
                    raise ProviderError("PROCESSING_CONFIG_CHANGED")
            else:
                session.add(
                    ProcessingSnapshot(
                        tenant_id=lease.tenant_id,
                        upload_id=lease.aggregate_id,
                        scoring_strategy=strategy,
                        fingerprint=fingerprint,
                        config=config,
                    )
                )
        return config

    def _extract(
        self, lease: Lease, *, cv_token_ceiling: int | None = None
    ) -> tuple[Extraction, CandidateProfile]:
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            if not upload:
                raise ProviderError("UPLOAD_NOT_FOUND")
            cached = session.scalar(
                select(ResumeExtraction).where(ResumeExtraction.upload_id == upload.id)
            )
            if cached:
                pages = [Page.model_validate(page) for page in cached.pages]
                if (
                    cv_token_ceiling is not None
                    and token_count("\n".join(page.text for page in pages)) > cv_token_ceiling
                ):
                    raise ProviderError("CV_TOKEN_CEILING_EXCEEDED")
                return (
                    Extraction(
                        pages=pages,
                        method=cached.method,
                        confidence=float(cached.confidence),
                    ),
                    CandidateProfile.model_validate(cached.profile),
                )
            file = session.get(ResumeFile, upload.file_id)
            assert file is not None
            content, mime_type = file.content, file.mime_type
            as_of = upload.source_received_at.date()
            upload.status, upload.review_reason = "extracting", None
        original_mime = mime_type
        if mime_type == DOC:
            content = self._convert(lease, content, mime_type)
            mime_type = PDF
        sanitized = False
        if mime_type == PDF:
            prepared = prepare_pdf(content, self.providers.scanner)
            sanitized = prepared is not content
            content = prepared
        extraction = self.providers.extractor.extract(content, mime_type)
        if needs_ocr(extraction):
            if mime_type != PDF:
                content = self._convert(lease, content, original_mime)
                prepared = prepare_pdf(content, self.providers.scanner)
                sanitized = prepared is not content
                content = prepared
            extraction = self.providers.ocr.extract(content)
        if sanitized:
            extraction.method += f"+{PROCESSING_VERSION}"
        full_text = "\n".join(page.text for page in extraction.pages)
        if not full_text.strip() or extraction.confidence < 0.8:
            raise ProviderError("LOW_CONFIDENCE_EXTRACTION")
        if len(full_text) > MAX_TEXT_CHARS:
            raise ProviderError("EXTRACTED_TEXT_TOO_LARGE")
        if cv_token_ceiling is not None and token_count(full_text) > cv_token_ceiling:
            raise ProviderError("CV_TOKEN_CEILING_EXCEEDED")
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            assert upload is not None
            upload.status = "profiling"
        profile = normalize_profile(self.providers.profiles.profile(full_text), as_of=as_of)
        if profile.language != "en":
            raise ProviderError("unsupported_language")
        if profile.confidence < 0.8:
            raise ProviderError("LOW_CONFIDENCE_PROFILE")
        with self.queue.owned(lease) as session:
            session.add(
                ResumeExtraction(
                    tenant_id=lease.tenant_id,
                    upload_id=lease.aggregate_id,
                    pages=[page.model_dump() for page in extraction.pages],
                    method=extraction.method,
                    confidence=Decimal(str(extraction.confidence)),
                    profile=profile.model_dump(),
                    profile_model=self.providers.profiles.model,
                    prompt_version="profile-v1",
                )
            )
        return extraction, profile

    def _convert(self, lease: Lease, content: bytes, mime_type: str) -> bytes:
        with self.queue.owned(lease) as session:
            cached = session.scalar(
                select(DocumentDerivative).where(DocumentDerivative.upload_id == lease.aggregate_id)
            )
            if cached:
                return cached.content
        converter = self.providers.converter
        if converter is None:
            raise ProviderError("CONVERTER_NOT_CONFIGURED")
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

    def _resolve(
        self, lease: Lease, profile: CandidateProfile, *, full_text: bool = False
    ) -> UUID | None:
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            assert upload is not None
            existing_resume = session.scalar(select(Resume).where(Resume.upload_id == upload.id))
            if existing_resume:
                return existing_resume.id
            identities = [
                (kind, value)
                for kind, value in (("email", profile.email), ("mobile", profile.mobile))
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
                                "mobile": profile.mobile,
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
                candidate.profile = {
                    **profile.model_dump(),
                    **resolved_contacts,
                    **candidate.contact_overrides,
                }
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
            upload.status = "matching" if full_text else "embedding"
            return resume.id

    def _embed(
        self,
        lease: Lease,
        resume_id: UUID,
        extraction: Extraction,
        profile: CandidateProfile,
        version_ids: list[UUID],
    ) -> None:
        self._snapshot(lease)
        with self.queue.owned(lease) as session:
            existing_chunk = session.scalar(
                select(ResumeChunk).where(ResumeChunk.resume_id == resume_id)
            )
            if existing_chunk and (
                existing_chunk.embedding_fingerprint != self.providers.fingerprint
                or existing_chunk.embedding_model != self.providers.embeddings.model
            ):
                raise ProviderError("EMBEDDING_MODEL_MISMATCH")
            criteria = session.scalars(
                select(JobCriterion)
                .where(JobCriterion.version_id.in_(version_ids))
                .order_by(JobCriterion.position)
            ).all()
            if any(
                item.embedding is not None
                and (
                    item.embedding_fingerprint != self.providers.fingerprint
                    or item.embedding_model != self.providers.embeddings.model
                )
                for item in criteria
            ):
                raise ProviderError("EMBEDDING_MODEL_MISMATCH")
            criterion_data = [(item.id, item.text) for item in criteria if item.embedding is None]
        if existing_chunk:
            if criterion_data:
                vectors = validate_vectors(
                    self.providers.embeddings.embed([value for _, value in criterion_data]),
                    len(criterion_data),
                )
                with self.queue.owned(lease) as session:
                    for (identifier, _), vector in zip(criterion_data, vectors, strict=True):
                        item = session.get(JobCriterion, identifier)
                        assert item is not None
                        item.embedding, item.embedding_model = (
                            vector,
                            self.providers.embeddings.model,
                        )
                        item.embedding_fingerprint = self.providers.fingerprint
            return
        candidates = chunk_pages(
            extraction.pages,
            self.queue.settings.chunk_size_tokens,
            self.queue.settings.chunk_overlap_tokens,
        )
        safe_pairs = [
            (
                item,
                scoring_text(
                    item.text, [profile.name or "", profile.email or "", profile.mobile or ""]
                ),
            )
            for item in candidates
        ]
        safe_pairs = [(item, safe) for item, safe in safe_pairs if safe.strip()]
        chunks = [item for item, _ in safe_pairs]
        masked = [safe for _, safe in safe_pairs]
        if not chunks:
            raise ProviderError("NO_SCORABLE_TEXT")
        values = masked + [text for _, text in criterion_data]
        batches = embedding_batches(
            values,
            self.queue.settings.embedding_batch_size,
            self.queue.settings.embedding_max_batch_tokens,
        )
        completed: dict[int, list[list[float]]] = {}
        hashes = [
            hashlib.sha256(json.dumps(batch, ensure_ascii=False).encode()).hexdigest()
            for batch in batches
        ]
        with self.queue.owned(lease) as session:
            for saved in session.scalars(
                select(EmbeddingBatch).where(EmbeddingBatch.upload_id == lease.aggregate_id)
            ):
                if (
                    saved.fingerprint != self.providers.fingerprint
                    or saved.input_hash != hashes[saved.position]
                ):
                    raise ProviderError("PROCESSING_CONFIG_CHANGED")
                completed[saved.position] = validate_vectors(
                    saved.vectors, len(batches[saved.position])
                )
        failure: Exception | None = None
        with ThreadPoolExecutor(
            max_workers=self.queue.settings.embedding_max_parallel_calls
        ) as pool:
            tasks = {
                pool.submit(self.providers.embeddings.embed, batch): i
                for i, batch in enumerate(batches)
                if i not in completed
            }
            for task in as_completed(tasks):
                position = tasks[task]
                try:
                    vectors = validate_vectors(task.result(), len(batches[position]))
                    with self.queue.owned(lease) as session:
                        session.add(
                            EmbeddingBatch(
                                tenant_id=lease.tenant_id,
                                upload_id=lease.aggregate_id,
                                position=position,
                                fingerprint=self.providers.fingerprint,
                                input_hash=hashes[position],
                                vectors=vectors,
                            )
                        )
                    completed[position] = vectors
                except Exception as exc:
                    failure = failure or exc
        if failure:
            raise failure
        logger.info("embedding.batches_completed batches=%s inputs=%s", len(batches), len(values))
        vectors = [vector for i in range(len(batches)) for vector in completed[i]]
        with self.queue.owned(lease) as session:
            for position, (chunk, safe_text, vector) in enumerate(
                zip(chunks, masked, vectors[: len(chunks)], strict=True)
            ):
                session.add(
                    ResumeChunk(
                        tenant_id=lease.tenant_id,
                        resume_id=resume_id,
                        position=position,
                        source_spans=chunk.source_spans,
                        text=chunk.text,
                        scoring_text=safe_text,
                        token_start=chunk.token_start,
                        token_end=chunk.token_end,
                        embedding=vector,
                        embedding_model=self.providers.embeddings.model,
                        embedding_fingerprint=self.providers.fingerprint,
                    )
                )
            for (criterion_id, _), vector in zip(
                criterion_data, vectors[len(chunks) :], strict=True
            ):
                row = session.get(JobCriterion, criterion_id)
                assert row is not None
                row.embedding, row.embedding_model = vector, self.providers.embeddings.model
                row.embedding_fingerprint = self.providers.fingerprint
            session.execute(
                delete(EmbeddingBatch).where(EmbeddingBatch.upload_id == lease.aggregate_id)
            )

    def process(self, lease: Lease) -> None:
        from recruitment.services.matching import MatchingWorkflow

        MatchingWorkflow(self.queue, self.providers, self).process(lease)
