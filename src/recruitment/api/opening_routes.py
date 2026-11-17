import json
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from recruitment.adapters.documents import MAX_BYTES
from recruitment.api.dependencies import Actor, Services
from recruitment.api.schemas import OpeningList, OpeningRead, Schema
from recruitment.core.errors import AppError
from recruitment.domain.openings import OpeningAction, OpeningCreate, OpeningStatus, OpeningUpdate
from recruitment.services.openings import JobFile

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["Openings"])
OptionalKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


class RevisionRequest(Schema):
    expected_revision: int = Field(ge=1)


@router.get("/openings", response_model=OpeningList)
def list_openings(
    tenant_id: UUID,
    actor: Actor,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    status: OpeningStatus | None = None,
) -> dict[str, Any]:
    return services.openings.list_openings(actor, tenant_id, limit, offset, status)


@router.post(
    "/openings",
    response_model=OpeningRead,
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": OpeningCreate.model_json_schema(
                        ref_template="#/components/schemas/{model}"
                    )
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["payload"],
                        "properties": {
                            "payload": {
                                "type": "string",
                                "description": "JSON-encoded OpeningCreate",
                            },
                            "files": {
                                "type": "array",
                                "maxItems": 5,
                                "items": {"type": "string", "format": "binary"},
                            },
                        },
                    }
                },
            },
        }
    },
)
async def create_opening(
    tenant_id: UUID,
    request: Request,
    actor: Actor,
    services: Services,
    response: Response,
    key: OptionalKey = None,
) -> dict[str, Any]:
    mime = request.headers.get("content-type", "").split(";")[0]
    files = []
    try:
        if mime == "application/json":
            raw = await request.body()
            if len(raw) > 512 * 1024:
                raise AppError("REQUEST_TOO_LARGE", "Job details are too large.", 413)
            payload = OpeningCreate.model_validate_json(raw)
        elif mime == "multipart/form-data":
            async with request.form(max_files=6, max_fields=2, max_part_size=512 * 1024) as form:
                if any(key not in {"payload", "files"} for key in form):
                    raise AppError("INVALID_OPENING", "Use payload and files for job uploads.")
                value = form.get("payload")
                if not isinstance(value, str):
                    raise AppError("INVALID_OPENING", "Include the JSON payload field.")
                payload = OpeningCreate.model_validate_json(value)
                for upload in form.getlist("files"):
                    if not isinstance(upload, StarletteUploadFile):
                        raise AppError("INVALID_DOCUMENT", "Attach a supported document.")
                    files.append(
                        JobFile(
                            upload.filename or "document",
                            upload.content_type or "",
                            await upload.read(MAX_BYTES + 1),
                        )
                    )
        else:
            raise AppError("UNSUPPORTED_MEDIA_TYPE", "Use JSON or multipart job details.", 415)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from None
    except json.JSONDecodeError:
        raise AppError("INVALID_OPENING", "Enter valid job details.") from None
    result, created = await run_in_threadpool(
        services.openings.create, actor, tenant_id, payload, files, key
    )
    if not created:
        response.status_code = 200
    return result


@router.get("/openings/{opening_id}", response_model=OpeningRead)
def read_opening(
    tenant_id: UUID, opening_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.openings.read(actor, tenant_id, opening_id)


@router.patch("/openings/{opening_id}", response_model=OpeningRead)
def update_opening(
    tenant_id: UUID, opening_id: UUID, payload: OpeningUpdate, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.openings.update(actor, tenant_id, opening_id, payload)


@router.post("/openings/{opening_id}/actions", response_model=OpeningRead)
def opening_action(
    tenant_id: UUID, opening_id: UUID, payload: OpeningAction, actor: Actor, services: Services
) -> dict[str, Any]:
    return services.openings.action(actor, tenant_id, opening_id, payload)


@router.get("/openings/{opening_id}/documents")
def list_documents(
    tenant_id: UUID, opening_id: UUID, actor: Actor, services: Services
) -> dict[str, Any]:
    return {"items": services.openings.read(actor, tenant_id, opening_id)["documents"]}


@router.post("/openings/{opening_id}/documents", response_model=OpeningRead, status_code=201)
def add_documents(
    tenant_id: UUID,
    opening_id: UUID,
    files: list[UploadFile],
    expected_revision: Annotated[int, Form(ge=1)],
    actor: Actor,
    services: Services,
) -> dict[str, Any]:
    try:
        values = [
            JobFile(f.filename or "document", f.content_type or "", f.file.read(MAX_BYTES + 1))
            for f in files
        ]
        return services.openings.documents(actor, tenant_id, opening_id, values, expected_revision)
    finally:
        for file in files:
            file.file.close()


@router.get("/openings/{opening_id}/documents/{document_id}/file")
def document_file(
    tenant_id: UUID, opening_id: UUID, document_id: UUID, actor: Actor, services: Services
) -> Response:
    item = services.openings.file(actor, tenant_id, opening_id, document_id)
    return Response(
        item.content,
        media_type=item.mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(item.file_name)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/openings/{opening_id}/documents/{document_id}", response_model=OpeningRead)
def remove_document(
    tenant_id: UUID,
    opening_id: UUID,
    document_id: UUID,
    expected_revision: Annotated[int, Query(ge=1)],
    actor: Actor,
    services: Services,
) -> dict[str, Any]:
    return services.openings.remove_document(
        actor, tenant_id, opening_id, document_id, expected_revision
    )
