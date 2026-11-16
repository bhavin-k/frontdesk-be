"""Fenced background preparation of immutable, source-backed job requirements."""

import time
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from recruitment.adapters.ai import validate_vectors
from recruitment.adapters.db.models import CriteriaVersion, CVUpload, JobCriterion, Opening
from recruitment.adapters.db.openings import OpeningDocument
from recruitment.adapters.documents import DOC, PDF, needs_ocr
from recruitment.adapters.pdf_safety import PROCESSING_VERSION
from recruitment.adapters.providers import Providers
from recruitment.core.errors import AppError, ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.matching import scoring_text
from recruitment.domain.openings import SHORTLIST_THRESHOLD, RequirementSource, checked_requirements
from recruitment.ports.queue import Lease, WorkerQueue
from recruitment.services.document_processing import prepare_pdf
from recruitment.services.openings import release_waiting


class OpeningPreparation:
    def __init__(self, queue: WorkerQueue, providers: Providers) -> None:
        self.queue, self.providers = queue, providers

    def process(self, lease: Lease) -> None:
        adapter = self.providers.job_criteria
        strategy = self.queue.settings.scoring_strategy
        config: dict[str, Any] = {
            **self.queue.settings.processing_snapshot(),
            "actual_provider": self.providers.name,
            "model": adapter.model if adapter else "unavailable",
            "prompt_version": "job-criteria-v1",
        }
        with self.queue.owned(lease) as session:
            row = session.scalar(
                select(Opening).where(Opening.id == lease.aggregate_id).with_for_update()
            )
            if not row:
                return
            if row.status == "closed":
                if row.criteria_status != "ready":
                    row.criteria_status, row.criteria_error = "failed", "OPENING_CLOSED"
                release_waiting(session, row, strategy)
                return
            if row.criteria_status == "ready":
                release_waiting(session, row, strategy)
                return
            revision = row.source_revision
            mismatch = bool(row.preparation_config and row.preparation_config != config)
            row.preparation_config = row.preparation_config or config
            expected_config = row.preparation_config
            row.criteria_status = "processing"
            original_sources = {"description": row.job_description}
            sources = [
                RequirementSource(id="description", text=scoring_text(row.job_description, []))
            ]
            for key in (
                "department",
                "location",
                "total_experience_years",
                "relevant_experience_years",
                "domain_experience",
                "candidate_requirements",
            ):
                value = row.details.get(key)
                if value is not None and str(value).strip():
                    text = f"{key.replace('_', ' ')}: {value}"
                    original_sources[f"details:{key}"] = text
                    sources.append(
                        RequirementSource(id=f"details:{key}", text=scoring_text(text, []))
                    )
            documents = [
                (d.id, d.content, d.mime_type, d.extracted_pages)
                for d in session.scalars(
                    select(OpeningDocument)
                    .where(OpeningDocument.opening_id == row.id)
                    .order_by(OpeningDocument.created_at, OpeningDocument.id)
                )
            ]
        started = time.monotonic()
        try:
            if adapter is None:
                raise ProviderError("JOB_CRITERIA_NOT_CONFIGURED")
            if mismatch:
                raise ProviderError("PROCESSING_CONFIG_CHANGED")
            pages_to_save = {}
            for document_id, content, mime, cached in documents:
                if cached:
                    pages = cached
                    method = None
                else:
                    original, original_mime = content, mime
                    if mime == DOC:
                        if not self.providers.converter:
                            raise ProviderError("CONVERTER_NOT_CONFIGURED")
                        content = self.providers.converter.convert(content, mime)
                        self.providers.scanner.scan(content)
                        mime = PDF
                    sanitized = False
                    if mime == PDF:
                        prepared = prepare_pdf(content, self.providers.scanner)
                        sanitized = prepared is not content
                        content = prepared
                    extraction = self.providers.extractor.extract(content, mime)
                    if needs_ocr(extraction):
                        if mime != PDF:
                            if not self.providers.converter:
                                raise ProviderError("CONVERTER_NOT_CONFIGURED")
                            content = self.providers.converter.convert(original, original_mime)
                            self.providers.scanner.scan(content)
                            prepared = prepare_pdf(content, self.providers.scanner)
                            sanitized = prepared is not content
                            content = prepared
                        extraction = self.providers.ocr.extract(content)
                    if sanitized:
                        extraction.method += f"+{PROCESSING_VERSION}"
                    if extraction.confidence < 0.8 or not any(
                        p.text.strip() for p in extraction.pages
                    ):
                        raise ProviderError("LOW_CONFIDENCE_JOB_DOCUMENT")
                    pages = [p.model_dump() for p in extraction.pages]
                    method = extraction.method
                    pages_to_save[document_id] = (pages, method)
                for index, page in enumerate(pages):
                    original_sources[f"document:{document_id}:page:{index}"] = page["text"]
                    sources.append(
                        RequirementSource(
                            id=f"document:{document_id}:page:{index}",
                            text=scoring_text(page["text"], []),
                        )
                    )
            if sum(len(s.text) for s in sources) > 200000:
                raise ProviderError("JOB_SOURCE_TOO_LARGE")
            result = checked_requirements(adapter.requirements(sources), sources)
            if any(
                evidence["quote"] not in original_sources[evidence["source_id"]]
                for _, evidence in result
            ):
                raise ProviderError("INVALID_JOB_EVIDENCE")
            vectors: list[list[float] | None] = [None] * len(result)
            if strategy == "rag":
                vectors[:] = validate_vectors(
                    self.providers.embeddings.embed([c.text for c, _ in result]), len(result)
                )
            with self.queue.owned(lease) as session:
                row = session.scalar(
                    select(Opening).where(Opening.id == lease.aggregate_id).with_for_update()
                )
                if not row or row.source_revision != revision or row.status == "closed":
                    return
                if (
                    row.preparation_config != config
                    or self.queue.settings.processing_snapshot()
                    != {k: config[k] for k in self.queue.settings.processing_snapshot()}
                ):
                    raise ProviderError("PROCESSING_CONFIG_CHANGED")
                if row.criteria_status == "ready":
                    return
                number = (
                    session.scalar(
                        select(func.max(CriteriaVersion.version)).where(
                            CriteriaVersion.opening_id == row.id
                        )
                    )
                    or 0
                )
                version = CriteriaVersion(
                    tenant_id=lease.tenant_id,
                    opening_id=row.id,
                    version=number + 1,
                    source_revision=revision,
                    shortlist_threshold=Decimal(SHORTLIST_THRESHOLD),
                    processing_metadata={
                        **config,
                        "processed_at": utcnow().isoformat(),
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    },
                )
                session.add(version)
                session.flush()
                for position, ((criterion, evidence), vector) in enumerate(
                    zip(result, vectors, strict=True)
                ):
                    session.add(
                        JobCriterion(
                            tenant_id=lease.tenant_id,
                            version_id=version.id,
                            position=position,
                            text=criterion.text,
                            required=criterion.required,
                            strict=criterion.strict,
                            weight=Decimal(1),
                            source_evidence=[evidence],
                            embedding=vector,
                            embedding_model=self.providers.embeddings.model if vector else None,
                            embedding_fingerprint=self.providers.fingerprint if vector else None,
                        )
                    )
                for id, (pages, method) in pages_to_save.items():
                    doc = session.get(OpeningDocument, id)
                    if doc:
                        doc.extracted_pages, doc.extraction_method = pages, method
                row.criteria_status, row.criteria_error = "ready", None
                from recruitment.services.matching import requeue_pending

                session.flush()
                requeue_pending(session, lease.tenant_id, f"prepared:{row.id}:{revision}", strategy)
                for upload in session.scalars(
                    select(CVUpload).where(
                        CVUpload.opening_id == row.id,
                        CVUpload.status == "needs_review",
                        CVUpload.review_reason == "JOB_REQUIREMENTS_FAILED",
                        CVUpload.retention_expires_at > utcnow(),
                    )
                ):
                    upload.status, upload.review_reason = "queued", None
                session.flush()
                release_waiting(session, row, strategy)
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, ProviderError)
                else ProviderError(
                    exc.code if isinstance(exc, AppError) else "INVALID_JOB_REQUIREMENTS"
                )
            )
            with self.queue.owned(lease) as session:
                row = session.scalar(
                    select(Opening).where(Opening.id == lease.aggregate_id).with_for_update()
                )
                if (
                    row
                    and row.source_revision == revision
                    and row.status != "closed"
                    and row.preparation_config == expected_config
                ):
                    retry = (
                        error.retryable and lease.attempts < self.queue.settings.max_job_attempts
                    )
                    row.criteria_status, row.criteria_error = (
                        ("queued" if retry else "failed"),
                        error.code,
                    )
                    if not retry:
                        for upload in session.scalars(
                            select(CVUpload).where(
                                CVUpload.opening_id == row.id, CVUpload.status == "queued"
                            )
                        ):
                            upload.status, upload.review_reason = (
                                "needs_review",
                                "JOB_REQUIREMENTS_FAILED",
                            )
            raise error from None
