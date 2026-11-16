from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import Member, RetentionSetting, Tenant
from recruitment.adapters.db.session import Database
from recruitment.core.errors import AppError
from recruitment.services.auth import Principal
from recruitment.services.common import emit, missing


class TenancyService:
    def __init__(self, db: Database) -> None:
        self.db = db

    @contextmanager
    def scope(self, principal: Principal, tenant_id: UUID) -> Iterator[Session]:
        with self.db.transaction(self.db.api, tenant_id, principal.user_id) as session:
            if not session.get(Tenant, tenant_id):
                raise missing()
            yield session

    def memberships(self, principal: Principal) -> list[dict[str, Any]]:
        with self.db.transaction(self.db.auth) as session:
            return [
                dict(row)
                for row in session.execute(
                    text("SELECT * FROM my_memberships(:actor)"), {"actor": principal.user_id}
                ).mappings()
            ]

    def create(self, principal: Principal, name: str) -> dict[str, Any]:
        with self.db.transaction(self.db.api, user_id=principal.user_id) as session:
            tenant_id = session.scalar(text("SELECT bootstrap_tenant(:name)"), {"name": name})
            return {"id": tenant_id, "name": name, "timezone": "Asia/Kolkata", "role": "recruiter"}

    def read(self, principal: Principal, tenant_id: UUID) -> dict[str, Any]:
        with self.scope(principal, tenant_id) as session:
            record = session.get(Tenant, tenant_id)
            assert record is not None
            return {
                "id": record.id,
                "name": record.name,
                "timezone": record.timezone,
                "role": "recruiter",
            }

    def members(self, principal: Principal, tenant_id: UUID) -> list[dict[str, Any]]:
        with self.scope(principal, tenant_id) as session:
            return [
                {"user_id": item.user_id, "role": item.role}
                for item in session.scalars(select(Member).limit(1000))
            ]

    def add_member(self, principal: Principal, tenant_id: UUID, user_id: UUID) -> dict[str, Any]:
        with self.scope(principal, tenant_id) as session:
            session.execute(
                text("SELECT add_tenant_member(:tenant, :member)"),
                {"tenant": tenant_id, "member": user_id},
            )
        return {"user_id": user_id, "role": "recruiter"}

    def remove_member(self, principal: Principal, tenant_id: UUID, user_id: UUID) -> None:
        with self.scope(principal, tenant_id) as session:
            if not session.scalar(
                text("SELECT remove_tenant_member(:tenant, :member)"),
                {
                    "tenant": tenant_id,
                    "member": user_id,
                },
            ):
                raise AppError("LAST_MEMBER", "Keep at least one recruiter in the business.", 409)

    def retention(
        self, principal: Principal, tenant_id: UUID, changes: dict[str, int] | None = None
    ) -> dict[str, int]:
        with self.scope(principal, tenant_id) as session:
            record = session.scalar(select(RetentionSetting).with_for_update())
            assert record is not None
            if changes:
                for name, value in changes.items():
                    setattr(record, name, value)
                emit(session, tenant_id, "retention.settings_changed", tenant_id)
            return {
                "rejected_retention_days": record.rejected_retention_days,
                "other_candidate_retention_days": record.other_candidate_retention_days,
            }
