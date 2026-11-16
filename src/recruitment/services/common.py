import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import IdempotencyRecord, Outbox
from recruitment.core.config import ScoringStrategy
from recruitment.core.errors import AppError


def advisory_lock(session: Session, scope: str) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": scope}
    )


def existing_request(
    session: Session, tenant_id: UUID, scope: str, key: str | None, payload: Any
) -> UUID | None:
    if not key:
        return None
    hashed_key = hashlib.sha256(key.encode()).hexdigest()
    advisory_lock(session, f"{tenant_id}:{scope}:{hashed_key}")
    record = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.key == hashed_key,
        )
    )
    if record:
        if record.fingerprint != fingerprint(payload):
            raise AppError("IDEMPOTENCY_CONFLICT", "The key was used for a different request.", 409)
        return record.resource_id
    return None


def fingerprint(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def remember_request(
    session: Session, tenant_id: UUID, scope: str, key: str | None, payload: Any, resource_id: UUID
) -> None:
    if key:
        session.add(
            IdempotencyRecord(
                tenant_id=tenant_id,
                scope=scope,
                key=hashlib.sha256(key.encode()).hexdigest(),
                fingerprint=fingerprint(payload),
                resource_id=resource_id,
            )
        )


def emit(
    session: Session,
    tenant_id: UUID,
    kind: str,
    aggregate_id: UUID,
    dedupe_key: str | None = None,
    *,
    scoring_strategy: ScoringStrategy = "rag",
) -> None:
    session.add(
        Outbox(
            id=uuid4(),
            tenant_id=tenant_id,
            kind=kind,
            scoring_strategy=scoring_strategy,
            aggregate_id=aggregate_id,
            dedupe_key=dedupe_key or f"{kind}:{aggregate_id}:{uuid4()}",
        )
    )


def missing() -> AppError:
    return AppError("RESOURCE_NOT_FOUND", "The requested record was not found.", 404)
