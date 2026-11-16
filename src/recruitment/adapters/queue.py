from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from secrets import randbelow

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import Application, CVUpload, FailedJob, Job, Resume
from recruitment.adapters.db.phase2 import MailAttachment, Mailbox
from recruitment.adapters.db.session import Database
from recruitment.core.config import Settings
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.ports.queue import Lease, LeaseLost
from recruitment.services.common import advisory_lock, emit


class PostgresJobQueue:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db, self.settings = db, settings

    def dispatch(self) -> int:
        with self.db.transaction(self.db.dispatcher) as session:
            session.execute(
                text("SELECT schedule_mailboxes(CAST(:methods AS text[]), :seconds)"),
                {
                    "methods": self.settings.enabled_mailbox_methods.split(",")
                    if self.settings.enabled_mailbox_methods
                    else [],
                    "seconds": self.settings.imap_poll_seconds,
                },
            )
            if self.settings.auto_delete_enabled:
                session.execute(text("SELECT schedule_retention()"))
                session.execute(text("SELECT schedule_mail_retention()"))
            return int(session.scalar(text("SELECT dispatch_outbox()")) or 0)

    def claim(self) -> Lease | None:
        with self.db.transaction(self.db.dispatcher) as session:
            row = (
                session.execute(
                    text("SELECT * FROM claim_job(:seconds)"),
                    {"seconds": self.settings.lease_seconds},
                )
                .mappings()
                .first()
            )
            return Lease(**dict(row)) if row else None

    @contextmanager
    def owned(self, lease: Lease) -> Iterator[Session]:
        with self.db.transaction(self.db.worker, lease.tenant_id) as session:
            advisory_lock(session, f"tenant:{lease.tenant_id}")
            job = session.scalar(select(Job).where(Job.id == lease.id).with_for_update())
            if (
                not job
                or job.state != "running"
                or job.lease_token != lease.lease_token
                or not job.lease_expires_at
                or job.lease_expires_at <= utcnow()
            ):
                raise LeaseLost
            yield session

    def renew(self, lease: Lease) -> bool:
        with self.db.transaction(self.db.worker, lease.tenant_id) as session:
            return bool(
                session.scalar(
                    text("""
                UPDATE job_runs SET lease_expires_at = now() + make_interval(secs => :seconds)
                WHERE id = :id AND lease_token = :token AND state = 'running'
                    AND lease_expires_at > now()
                RETURNING id
            """),
                    {
                        "id": lease.id,
                        "token": lease.lease_token,
                        "seconds": self.settings.lease_seconds,
                    },
                )
            )

    @contextmanager
    def serialize_cv_scoring(self, lease: Lease) -> Iterator[None]:
        # A separate connection lets the worker heartbeat and retention proceed.
        with self.db.transaction(self.db.worker, lease.tenant_id) as session:
            advisory_lock(session, f"cv-scoring:{lease.tenant_id}:{lease.aggregate_id}")
            yield

    def finish(self, lease: Lease) -> None:
        try:
            with self.owned(lease) as session:
                job = session.get(Job, lease.id)
                assert job is not None
                job.state, job.lease_token, job.lease_expires_at = "succeeded", None, None
                job.failure_code = None
        except LeaseLost:
            # Retention removes the job and all candidate-linked operational rows together.
            return

    def fail(self, lease: Lease, error: ProviderError) -> None:
        with self.owned(lease) as session:
            job = session.get(Job, lease.id)
            assert job is not None
            retry = error.retryable and lease.attempts < self.settings.max_job_attempts
            job.state = "retry_wait" if retry else "failed"
            job.failure_code = error.code
            job.lease_token = None
            job.lease_expires_at = None
            job.available_at = utcnow() + timedelta(
                seconds=max(
                    min(3600, 10 * 2 ** min(lease.attempts, 8)) + randbelow(10),
                    error.retry_after_seconds or 0,
                )
            )
            if lease.kind == "cv.uploaded":
                upload = session.get(CVUpload, lease.aggregate_id)
                if upload:
                    upload.status = "retry_wait" if retry else "needs_review"
                    if upload.review_reason not in {"REAPPLICATION", "RESUME_UPDATED"}:
                        upload.review_reason = error.code
                    if upload.application_id:
                        app = session.get(Application, upload.application_id)
                        current = (
                            session.get(Resume, app.current_resume_id)
                            if app and app.current_resume_id
                            else None
                        )
                        if (
                            app
                            and not app.rejected_at
                            and (not current or current.upload_id == upload.id)
                        ):
                            app.status = "needs_review"
                    if not retry:
                        if upload.application_id:
                            from recruitment.adapters.db.matching import (
                                MatchingEvaluation,
                                RoleAssessment,
                            )

                            current_app = session.get(Application, upload.application_id)
                            evaluation = (
                                session.get(MatchingEvaluation, current_app.latest_evaluation_id)
                                if current_app and current_app.latest_evaluation_id
                                else None
                            )
                            if (
                                current_app
                                and evaluation
                                and current
                                and current.upload_id == upload.id
                                and evaluation.resume_id == current.id
                                and evaluation.state == "processing"
                            ):
                                evaluation.state, evaluation.reason, evaluation.confidence = (
                                    "completed",
                                    "Experience or matching processing could not be completed.",
                                    None,
                                )
                                evaluation.processed_at = utcnow()
                                for role in session.scalars(
                                    select(RoleAssessment).where(
                                        RoleAssessment.evaluation_id == evaluation.id,
                                        RoleAssessment.state.in_(["pending", "processing"]),
                                    )
                                ):
                                    role.state, role.failure_code = "failed", error.code
                                    role.recommendation, role.confidence, role.score = (
                                        "needs_review",
                                        None,
                                        None,
                                    )
                                    role.reason = "Processing could not be completed for this role."
                        emit(session, lease.tenant_id, "review.required", upload.id)
            elif lease.kind == "mail.attachment":
                attachment = session.get(MailAttachment, lease.aggregate_id)
                if attachment:
                    attachment.status = "retry_wait" if retry else "needs_review"
                    attachment.failure_code = error.code
                    if not retry:
                        emit(session, lease.tenant_id, "review.required", attachment.id)
            elif lease.kind in {"mailbox.sync", "mailbox.maintain"}:
                connection = session.get(Mailbox, lease.aggregate_id)
                if connection and connection.status == "connected":
                    connection.failure_code = error.code
                    connection.sync_status = "retry_wait" if retry else "failed"
                    if error.code == "MAILBOX_REAUTHORIZATION_REQUIRED":
                        connection.status = "reauthorization_required"
                        connection.lease_token = connection.lease_expires_at = None
            if not retry:
                session.add(
                    FailedJob(
                        tenant_id=lease.tenant_id,
                        job_id=lease.id,
                        aggregate_id=lease.aggregate_id,
                        failure_code=error.code,
                    )
                )


class MemoryJobQueue:
    """Inert queue for unit tests of dispatching clients."""

    def __init__(self) -> None:
        self.pending: list[Lease] = []
        self.done: list[Lease] = []

    def dispatch(self) -> int:
        return len(self.pending)

    def claim(self) -> Lease | None:
        return self.pending.pop(0) if self.pending else None

    def renew(self, lease: Lease) -> bool:
        return lease not in self.done

    def finish(self, lease: Lease) -> None:
        self.done.append(lease)
