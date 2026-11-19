from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from recruitment.core.config import Settings
from recruitment.core.errors import ProviderError


class LeaseLost(Exception):
    pass


@dataclass(frozen=True)
class Lease:
    id: UUID
    tenant_id: UUID
    kind: str
    aggregate_id: UUID
    lease_token: UUID
    attempts: int


class JobQueue(Protocol):
    def dispatch(self) -> int: ...
    def claim(self) -> Lease | None: ...
    def renew(self, lease: Lease) -> bool: ...
    def finish(self, lease: Lease) -> None: ...


class WorkerQueue(JobQueue, Protocol):
    """Queue capability that fences canonical PostgreSQL state transitions."""

    @property
    def settings(self) -> Settings: ...
    def owned(self, lease: Lease) -> AbstractContextManager[Session]: ...
    def rubric_scope(self, lease: Lease) -> AbstractContextManager[Session]: ...
    def serialize_cv_scoring(self, lease: Lease) -> AbstractContextManager[None]: ...
    def serialize_screening(
        self,
        lease: Lease,
        request_id: UUID,
    ) -> AbstractContextManager[None]: ...
    def fail(self, lease: Lease, error: ProviderError) -> None: ...
