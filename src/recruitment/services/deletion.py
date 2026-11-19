from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import (
    Application,
    Candidate,
    CVUpload,
    FailedJob,
    IdempotencyRecord,
    Job,
    MatchRun,
    Outbox,
    Resume,
    ResumeFile,
    RetentionSetting,
    Tombstone,
)
from recruitment.adapters.db.phase2 import IdentityReview, MailAttachment, MailMessage
from recruitment.adapters.db.screening import ScreeningMessage, ScreeningRequest, ScreeningThread
from recruitment.core.time import utcnow
from recruitment.domain.retention import deadline
from recruitment.ports.queue import Lease, WorkerQueue


def clear_operational_rows(session: Session, ids: list[UUID]) -> None:
    for model in (Outbox, Job, FailedJob):
        session.execute(delete(model).where(model.aggregate_id.in_(ids)))
    session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.resource_id.in_(ids)))


def clear_mail_links(session: Session, uploads: list[UUID]) -> None:
    ids = list(
        session.scalars(select(MailAttachment.id).where(MailAttachment.upload_id.in_(uploads)))
    )
    ids.extend(
        session.scalars(select(IdentityReview.id).where(IdentityReview.upload_id.in_(uploads)))
    )
    if ids:
        clear_operational_rows(session, ids)


def purge_orphan_messages(session: Session) -> None:
    session.execute(
        delete(MailMessage).where(
            ~exists(select(MailAttachment.id).where(MailAttachment.message_id == MailMessage.id)),
            ~exists(select(IdentityReview.id).where(IdentityReview.message_id == MailMessage.id)),
        )
    )


class RetentionService:
    def __init__(self, queue: WorkerQueue) -> None:
        self.queue = queue

    def recalculate(self, lease: Lease) -> None:
        with self.queue.owned(lease) as session:
            setting = session.scalar(select(RetentionSetting))
            assert setting is not None
            for app in session.scalars(select(Application).with_for_update()):
                app.retention_expires_at = deadline(
                    app.last_activity_at,
                    app.rejected_at,
                    setting.rejected_retention_days,
                    setting.other_candidate_retention_days,
                )
            for upload in session.scalars(select(CVUpload).with_for_update()):
                if upload.application_id:
                    linked_application = session.get(Application, upload.application_id)
                    assert linked_application is not None
                    upload.retention_expires_at = linked_application.retention_expires_at
                else:
                    upload.retention_expires_at = deadline(
                        upload.created_at, None, other_days=setting.other_candidate_retention_days
                    )

    def delete_application(self, lease: Lease) -> None:
        if not self.queue.settings.auto_delete_enabled:
            return
        with self.queue.owned(lease) as session:
            app = session.scalar(
                select(Application).where(Application.id == lease.aggregate_id).with_for_update()
            )
            if not app:
                return
            settings = session.scalar(select(RetentionSetting))
            assert settings is not None
            app.retention_expires_at = deadline(
                app.last_activity_at,
                app.rejected_at,
                settings.rejected_retention_days,
                settings.other_candidate_retention_days,
            )
            if app.retention_expires_at > utcnow():
                return
            uploads = session.scalars(
                select(CVUpload).where(CVUpload.application_id == app.id)
            ).all()
            file_ids = [upload.file_id for upload in uploads]
            ids = [app.id, *[upload.id for upload in uploads], *file_ids]
            ids.extend(session.scalars(select(Resume.id).where(Resume.application_id == app.id)))
            ids.extend(
                session.scalars(select(MatchRun.id).where(MatchRun.application_id == app.id))
            )
            screenings = list(
                session.scalars(
                    select(ScreeningRequest.id).where(
                        ScreeningRequest.application_id == app.id,
                    )
                )
            )
            ids.extend(screenings)
            ids.extend(
                session.scalars(
                    select(ScreeningMessage.id).where(
                        ScreeningMessage.thread_id.in_(
                            select(ScreeningThread.id).where(
                                ScreeningThread.request_id.in_(screenings),
                            )
                        ),
                    )
                )
            )
            candidate_id = app.candidate_id
            session.add(
                Tombstone(
                    tenant_id=lease.tenant_id,
                    application_id=app.id,
                    role_id=app.opening_id,
                    final_status=app.status,
                )
            )
            clear_operational_rows(session, ids)
            clear_mail_links(session, [upload.id for upload in uploads])
            session.execute(delete(Application).where(Application.id == app.id))
            session.execute(delete(ResumeFile).where(ResumeFile.id.in_(file_ids)))
            if not session.scalar(
                select(Application.id).where(Application.candidate_id == candidate_id).limit(1)
            ) and not session.scalar(
                select(CVUpload.id).where(CVUpload.resolved_candidate_id == candidate_id).limit(1)
            ):
                cases = list(
                    session.scalars(
                        select(IdentityReview.id).where(IdentityReview.candidate_id == candidate_id)
                    )
                )
                clear_operational_rows(session, cases)
                for case in session.scalars(select(IdentityReview)):
                    if str(candidate_id) in case.candidate_options:
                        case.candidate_options = [
                            value for value in case.candidate_options if value != str(candidate_id)
                        ]
                session.execute(delete(Candidate).where(Candidate.id == candidate_id))
            purge_orphan_messages(session)

    def delete_upload(self, lease: Lease) -> None:
        if not self.queue.settings.auto_delete_enabled:
            return
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            if not upload or upload.application_id:
                return
            settings = session.scalar(select(RetentionSetting))
            assert settings is not None
            upload.retention_expires_at = deadline(
                upload.created_at,
                None,
                other_days=settings.other_candidate_retention_days,
            )
            if upload.retention_expires_at > utcnow():
                return
            clear_operational_rows(session, [upload.id, upload.file_id])
            clear_mail_links(session, [upload.id])
            session.execute(delete(ResumeFile).where(ResumeFile.id == upload.file_id))
            purge_orphan_messages(session)

    def delete_mail_source(self, lease: Lease) -> None:
        if not self.queue.settings.auto_delete_enabled:
            return
        with self.queue.owned(lease) as session:
            source = session.get(MailMessage, lease.aggregate_id)
            policy = session.scalar(select(RetentionSetting))
            if (
                not source
                or not policy
                or source.received_at + timedelta(days=policy.other_candidate_retention_days)
                > utcnow()
            ):
                return
            attachments = list(
                session.scalars(
                    select(MailAttachment).where(MailAttachment.message_id == source.id)
                )
            )
            for item in attachments:
                upload = session.get(CVUpload, item.upload_id) if item.upload_id else None
                if upload and upload.retention_expires_at > utcnow():
                    return
            ids = [source.id, *[item.id for item in attachments]]
            ids.extend(
                session.scalars(
                    select(IdentityReview.id).where(IdentityReview.message_id == source.id)
                )
            )
            clear_operational_rows(session, ids)
            session.execute(delete(MailMessage).where(MailMessage.id == source.id))
