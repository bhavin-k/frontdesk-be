"""Fresh dashboard reads sharing one authentication transaction."""

from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from recruitment.core.errors import AppError
from recruitment.services.auth import AuthService, Principal
from recruitment.services.candidates import candidate_page
from recruitment.services.tenancy import TenancyService


class DashboardService:
    def __init__(self, auth: AuthService, tenants: TenancyService) -> None:
        self.auth, self.tenants = auth, tenants

    def bootstrap(
        self, token: str, tenant_id: UUID | None
    ) -> tuple[dict[str, Any], dict[str, float]]:
        timings = {}
        started = perf_counter()
        db = self.tenants.db
        # The API task creates its own session and independently enforces tenant RLS.
        # No Session or connection is shared between threads, and nothing is published
        # until both the membership lookup and the candidate authorization succeed.
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.transaction(db.auth) as session:
                timings["auth_checkout"] = perf_counter() - started
                stage = perf_counter()
                actor = self.auth.authenticate_in_session(session, token)
                timings["authentication"] = perf_counter() - stage
                pending = pool.submit(self.read_candidates, actor, tenant_id) if tenant_id else None
                stage = perf_counter()
                memberships = self.tenants.memberships_in_session(session, actor)
                timings["memberships"] = perf_counter() - stage
            prefetched = pending.result() if pending else None
        selected = (
            next(
                (item["tenant_id"] for item in memberships if item["tenant_id"] == tenant_id), None
            )
            if tenant_id
            else (memberships[0]["tenant_id"] if memberships else None)
        )
        result: dict[str, Any] = {
            "identity": {"user_id": actor.user_id, "memberships": memberships},
            "tenant_id": selected,
            "candidates": None,
            "candidate_error": None,
        }
        if tenant_id and selected is None:
            result["candidate_error"] = {
                "code": "RESOURCE_NOT_FOUND",
                "message": "This business is unavailable.",
            }
        elif selected is not None:
            data, error, candidate_timings = prefetched or self.read_candidates(actor, selected)
            result["candidates"], result["candidate_error"] = data, error
            timings.update(candidate_timings)
        timings["total"] = perf_counter() - started
        return result, timings

    def read_candidates(
        self, actor: Principal, tenant_id: UUID
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None, dict[str, float]]:
        stage = perf_counter()
        timings = {}
        try:
            with self.tenants.scope(actor, tenant_id) as session:
                timings["candidate_checkout"] = perf_counter() - stage
                stage = perf_counter()
                data = {"items": candidate_page(session, 20, 0), "limit": 20, "offset": 0}
                timings["candidates"] = perf_counter() - stage
            return data, None, timings
        except (AppError, SQLAlchemyError):
            return (
                None,
                {
                    "code": "CANDIDATES_UNAVAILABLE",
                    "message": "Candidate data could not be loaded. Retry shortly.",
                },
                timings,
            )
