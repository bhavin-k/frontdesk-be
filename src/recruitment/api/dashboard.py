from uuid import UUID

from fastapi import APIRouter, Request, Response

from recruitment.api.dependencies import Services, SessionToken
from recruitment.api.intake_schemas import CandidateList
from recruitment.api.schemas import MeRead, Schema
from recruitment.services.dashboard import DashboardService

router = APIRouter(prefix="/api/v1", tags=["Dashboard"])


class CandidateLoadError(Schema):
    code: str
    message: str


class DashboardRead(Schema):
    identity: MeRead
    tenant_id: UUID | None
    candidates: CandidateList | None
    candidate_error: CandidateLoadError | None


@router.get("/dashboard-bootstrap", response_model=DashboardRead)
def bootstrap(
    services: Services,
    token: SessionToken,
    request: Request,
    response: Response,
    tenant_id: UUID | None = None,
) -> DashboardRead:
    data, timings = DashboardService(services.auth, services.tenants).bootstrap(token, tenant_id)
    if request.app.state.performance_diagnostics:
        response.headers["Server-Timing"] = ", ".join(
            f"{name};dur={duration * 1000:.1f}" for name, duration in timings.items()
        )
    return DashboardRead.model_validate(data)
