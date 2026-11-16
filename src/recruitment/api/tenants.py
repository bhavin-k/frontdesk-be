from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response

from recruitment.api.dependencies import Actor, Services
from recruitment.api.schemas import (
    MemberCreate,
    MemberRead,
    RetentionPatch,
    RetentionRead,
    TenantCreate,
    TenantRead,
)

router = APIRouter(prefix="/api/v1/tenants", tags=["Businesses"])


@router.post("", response_model=TenantRead, status_code=201)
def create_tenant(payload: TenantCreate, actor: Actor, services: Services) -> dict[str, Any]:
    return services.tenants.create(actor, payload.name)


@router.get("/{tenant_id}", response_model=TenantRead)
def get_tenant(tenant_id: UUID, actor: Actor, services: Services) -> dict[str, Any]:
    return services.tenants.read(actor, tenant_id)


@router.get("/{tenant_id}/members", response_model=list[MemberRead])
def members(tenant_id: UUID, actor: Actor, services: Services) -> list[dict[str, Any]]:
    return services.tenants.members(actor, tenant_id)


@router.post("/{tenant_id}/members", response_model=MemberRead, status_code=201)
def add_member(
    tenant_id: UUID, payload: MemberCreate, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.tenants.add_member(actor, tenant_id, payload.user_id)


@router.delete("/{tenant_id}/members/{user_id}", status_code=204)
def remove_member(tenant_id: UUID, user_id: UUID, actor: Actor, services: Services) -> Response:
    services.tenants.remove_member(actor, tenant_id, user_id)
    return Response(status_code=204)


@router.get("/{tenant_id}/retention-settings", response_model=RetentionRead)
def retention_settings(tenant_id: UUID, actor: Actor, services: Services) -> dict[str, int]:
    return services.tenants.retention(actor, tenant_id)


@router.patch("/{tenant_id}/retention-settings", response_model=RetentionRead)
def update_retention(
    tenant_id: UUID, payload: RetentionPatch, actor: Actor, services: Services
) -> dict[str, int]:
    return services.tenants.retention(
        actor, tenant_id, payload.model_dump(exclude_unset=True, exclude_none=True)
    )
