from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from recruitment.api.dependencies import Actor, Services
from recruitment.api.recruitment import RequestKey
from recruitment.services.screening_dashboard import InvitationCopy, ScreeningActionInput

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["Screening"])


@router.get("/screening-requests")
def requests(
    tenant_id: UUID,
    actor: Actor,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    return services.screening.list(actor, tenant_id, limit, offset)


@router.get("/screening-requests/{request_id}")
def detail(tenant_id: UUID, request_id: UUID, actor: Actor, services: Services) -> dict[str, Any]:
    return services.screening.detail(actor, tenant_id, request_id)


@router.post("/screening-requests/{request_id}/actions")
def actions(
    tenant_id: UUID,
    request_id: UUID,
    payload: ScreeningActionInput,
    key: RequestKey,
    actor: Actor,
    services: Services,
) -> dict[str, Any]:
    return services.screening.act(actor, tenant_id, request_id, payload, key)


@router.get("/screening-settings")
def invitation_copy(tenant_id: UUID, actor: Actor, services: Services) -> InvitationCopy:
    return services.screening.edit_copy(actor, tenant_id)


@router.patch("/screening-settings")
def change_invitation_copy(
    tenant_id: UUID,
    payload: InvitationCopy,
    actor: Actor,
    services: Services,
) -> InvitationCopy:
    return services.screening.edit_copy(actor, tenant_id, payload)
