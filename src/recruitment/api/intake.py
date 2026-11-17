from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Response

from recruitment.api.dependencies import Actor, Services
from recruitment.api.intake_schemas import (
    CandidateList,
    CandidateRead,
    ContactPatch,
    FolderList,
    IdentityList,
    IdentityRead,
    IdentityResolve,
    IMAPConnect,
    IMAPCredentials,
    IntakeList,
    MailboxCreate,
    MailboxList,
    MailboxPatch,
    MailboxRead,
    MailProviderList,
    OAuthCallback,
    Provider,
    ResumeVersionList,
)
from recruitment.api.recruitment import RequestKey

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["CV intake"])
oauth = APIRouter(prefix="/api/v1/mailbox-oauth", tags=["Mailbox OAuth"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]


@router.get("/mailbox-providers", response_model=MailProviderList)
def providers(tenant_id: UUID, actor: Actor, services: Services) -> dict[str, Any]:
    return services.mailboxes.capabilities(actor, tenant_id)


@router.get("/mailboxes", response_model=MailboxList)
def mailboxes(
    tenant_id: UUID, actor: Actor, services: Services, limit: Limit = 20, offset: Offset = 0
) -> dict[str, Any]:
    return services.mailboxes.list(actor, tenant_id, limit, offset)


@router.post("/mailboxes", response_model=MailboxRead, status_code=201)
def connect(
    tenant_id: UUID, payload: MailboxCreate, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.mailboxes.connect(actor, tenant_id, payload.provider, payload.opening_id)


@router.get("/mailboxes/{connection_id}", response_model=MailboxRead)
def mailbox(
    tenant_id: UUID, connection_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.mailboxes.get(actor, tenant_id, connection_id)


@router.post("/mailboxes/imap", response_model=MailboxRead, status_code=201)
def connect_imap(
    tenant_id: UUID, payload: IMAPConnect, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.mailboxes.connect_imap(
        actor, tenant_id, payload.opening_id, payload.email, payload.app_password.get_secret_value()
    )


@router.post("/mailboxes/{connection_id}/imap-credentials", response_model=MailboxRead)
def imap_credentials(
    tenant_id: UUID, connection_id: UUID, payload: IMAPCredentials, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.mailboxes.refresh_imap(
        actor,
        tenant_id,
        connection_id,
        payload.expected_generation,
        payload.app_password.get_secret_value(),
    )


@router.patch("/mailboxes/{connection_id}", response_model=MailboxRead)
def configure(
    tenant_id: UUID, connection_id: UUID, payload: MailboxPatch, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.mailboxes.configure(
        actor, tenant_id, connection_id, payload.model_dump(exclude_none=True)
    )


@router.delete("/mailboxes/{connection_id}", response_model=MailboxRead, status_code=202)
def disconnect(
    tenant_id: UUID, connection_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.mailboxes.disconnect(actor, tenant_id, connection_id)


@router.get("/mailboxes/{connection_id}/folders", response_model=FolderList)
def folders(
    tenant_id: UUID, connection_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.mailboxes.folders(actor, tenant_id, connection_id)


@router.post("/mailboxes/{connection_id}/sync", response_model=MailboxRead, status_code=202)
def synchronize(
    tenant_id: UUID, connection_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.mailboxes.sync(actor, tenant_id, connection_id)


@oauth.post("/{provider}/callback", response_model=MailboxRead)
def callback(
    provider: Provider, payload: OAuthCallback, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.mailboxes.callback(actor, provider, payload.state, payload.code, payload.error)


@router.get("/mail-intake", response_model=IntakeList)
def intake(
    tenant_id: UUID, actor: Actor, services: Services, limit: Limit = 20, offset: Offset = 0
) -> dict[str, Any]:
    return services.candidates.intake(actor, tenant_id, limit, offset)


@router.get("/candidates", response_model=CandidateList)
def candidates(
    tenant_id: UUID, actor: Actor, services: Services, limit: Limit = 20, offset: Offset = 0
) -> dict[str, Any]:
    return services.candidates.list(actor, tenant_id, limit, offset)


@router.get("/candidates/{candidate_id}", response_model=CandidateRead)
def candidate(
    tenant_id: UUID, candidate_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.candidates.get(actor, tenant_id, candidate_id)


@router.patch("/candidates/{candidate_id}", response_model=CandidateRead)
def correct(
    tenant_id: UUID, candidate_id: UUID, payload: ContactPatch, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.candidates.patch(
        actor, tenant_id, candidate_id, payload.model_dump(exclude_unset=True)
    )


@router.get("/candidates/{candidate_id}/resumes", response_model=ResumeVersionList)
def resumes(
    tenant_id: UUID,
    candidate_id: UUID,
    actor: Actor,
    services: Services,
    limit: Limit = 20,
    offset: Offset = 0,
) -> dict[str, Any]:
    return services.candidates.resumes(actor, tenant_id, candidate_id, limit, offset)


@router.get(
    "/cv-uploads/{upload_id}/file",
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "Original CV, attachment download",
        }
    },
)
def original(tenant_id: UUID, upload_id: UUID, actor: Actor, services: Services) -> Response:
    content, mime = services.candidates.file(actor, tenant_id, upload_id)
    extension = {
        "application/pdf": "pdf",
        "application/msword": "doc",
        "image/jpeg": "jpg",
        "image/png": "png",
    }.get(mime, "docx")
    return Response(
        content,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="cv.{extension}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/identity-reviews", response_model=IdentityList)
def reviews(
    tenant_id: UUID, actor: Actor, services: Services, limit: Limit = 20, offset: Offset = 0
) -> dict[str, Any]:
    return services.candidates.reviews(actor, tenant_id, limit, offset)


@router.get("/identity-reviews/{review_id}", response_model=IdentityRead)
def review(tenant_id: UUID, review_id: UUID, actor: Actor, services: Services) -> dict[str, Any]:
    return services.candidates.review(actor, tenant_id, review_id)


@router.post("/identity-reviews/{review_id}/resolve", response_model=IdentityRead, status_code=202)
def resolve(
    tenant_id: UUID,
    review_id: UUID,
    payload: IdentityResolve,
    key: RequestKey,
    actor: Actor,
    services: Services,
) -> dict[str, Any]:
    return services.candidates.resolve(
        actor, tenant_id, review_id, payload.model_dump(exclude_none=True), key
    )
