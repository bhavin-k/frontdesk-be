from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import ResumeFile
from recruitment.core.errors import AppError


class PostgresFileStorage:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session, self.tenant_id = session, tenant_id

    def store(self, content: bytes, mime_type: str) -> str:
        digest = sha256(content).hexdigest()
        existing = self.session.scalar(
            select(ResumeFile).where(
                ResumeFile.tenant_id == self.tenant_id, ResumeFile.sha256 == digest
            )
        )
        if existing:
            return str(existing.id)
        record = ResumeFile(
            id=uuid4(),
            tenant_id=self.tenant_id,
            sha256=digest,
            content=content,
            mime_type=mime_type,
            byte_length=len(content),
        )
        self.session.add(record)
        self.session.flush()
        return str(record.id)

    def read(self, file_id: str) -> bytes:
        record = self.session.get(ResumeFile, UUID(file_id))
        if not record or record.tenant_id != self.tenant_id:
            raise AppError("RESOURCE_NOT_FOUND", "The requested record was not found.", 404)
        return record.content
