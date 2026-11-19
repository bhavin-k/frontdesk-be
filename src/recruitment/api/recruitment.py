from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response, UploadFile

from recruitment.adapters.documents import MAX_BYTES
from recruitment.api.dependencies import Actor, Services
from recruitment.api.schemas import (
    ApplicationRead,
    AssessmentDetail,
    MatchResponse,
    OverviewRead,
    ReviewCreate,
    ReviewQueue,
    ReviewRead,
    UploadList,
    UploadRead,
)

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["Recruitment"])
RequestKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
OptionalKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


@router.get(
    "/applications/{application_id}/role-assessments/{assessment_id}",
    response_model=AssessmentDetail,
)
def assessment(
    tenant_id: UUID, application_id: UUID, assessment_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.recruitment.assessment(actor, tenant_id, application_id, assessment_id)


@router.get("/overview", response_model=OverviewRead)
def overview(
    tenant_id: UUID,
    actor: Actor,
    services: Services,
    opening_id: UUID | None = None,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> dict[str, Any]:
    return services.overview.read(actor, tenant_id, opening_id, days)


@router.get("/cv-uploads", response_model=UploadList)
def list_uploads(
    tenant_id: UUID,
    actor: Actor,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    opening_id: UUID | None = None,
) -> dict[str, Any]:
    return services.recruitment.uploads(actor, tenant_id, limit, offset, opening_id)


@router.post("/openings/{opening_id}/cv-uploads", response_model=UploadRead, status_code=202)
def upload(
    tenant_id: UUID,
    opening_id: UUID,
    file: UploadFile,
    key: RequestKey,
    actor: Actor,
    services: Services,
    response: Response,
) -> dict[str, Any]:
    # Routes are synchronous so validation and local extraction never block the ASGI loop.
    content = file.file.read(MAX_BYTES + 1)
    try:
        result, created = services.recruitment.upload(
            actor, tenant_id, opening_id, content, file.content_type or "", key
        )
    finally:
        file.file.close()
    if not created:
        response.status_code = 200
    return result


@router.get("/cv-uploads/{upload_id}", response_model=UploadRead)
def upload_status(
    tenant_id: UUID, upload_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.recruitment.upload_status(actor, tenant_id, upload_id)


@router.get("/applications/{application_id}", response_model=ApplicationRead)
def application(
    tenant_id: UUID, application_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.recruitment.application(actor, tenant_id, application_id)


@router.get("/applications/{application_id}/match-results", response_model=MatchResponse)
def match_results(
    tenant_id: UUID, application_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.recruitment.results(actor, tenant_id, application_id)


@router.get("/review-queue", response_model=ReviewQueue)
def review_queue(
    tenant_id: UUID,
    actor: Actor,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> dict[str, Any]:
    return services.recruitment.review_queue(actor, tenant_id, limit, offset)


@router.post(
    "/applications/{application_id}/review-decisions", response_model=ReviewRead, status_code=201
)
def decide(
    tenant_id: UUID,
    application_id: UUID,
    payload: ReviewCreate,
    key: RequestKey,
    actor: Actor,
    services: Services,
    response: Response,
) -> dict[str, Any]:
    if payload.evaluation_id:
        from recruitment.services.evaluation_review import decide_evaluation

        assert payload.expected_version is not None
        result, created = decide_evaluation(
            services.tenants,
            actor,
            tenant_id,
            application_id,
            payload.evaluation_id,
            payload.expected_version,
            payload.role_assessment_id,
            payload.decision,
            payload.confirm_rejection,
            payload.reason,
            key,
            services.recruitment.settings,
        )
        if not created:
            response.status_code = 200
        return result
    assert payload.match_run_id is not None
    result, created = services.recruitment.decide(
        actor,
        tenant_id,
        application_id,
        payload.match_run_id,
        payload.decision,
        payload.confirm_rejection,
        payload.reason,
        key,
    )
    if not created:
        response.status_code = 200
    return result
